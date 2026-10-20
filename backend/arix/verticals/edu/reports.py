"""Owner reports for an education centre (ТЗ B5 У14)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core import events as ev
from arix.core.context import TenantContext
from arix.core.timeutil import MONTHS_RU, combine_local, local_date, now_utc, to_tenant
from arix.db.models_core import DailyMetric, Event, Handoff, Payment

from . import attendance as att
from . import billing as bl
from . import enrollment as en
from .models import Course, Group, Lesson, Student, TrialBooking, TrialLesson


@dataclass
class DailyReport:
    day: date
    conversations: int = 0
    first_response_p50: float | None = None
    first_response_p95: float | None = None
    trials_booked: int = 0
    trials_attended: int = 0
    trials_no_show: int = 0
    paid_after_trial: int = 0
    enrollments: int = 0
    groups: list[dict] = field(default_factory=list)
    debt_students: int = 0
    debt_total: float = 0.0
    debt_currency: str = "UZS"
    debt_long_overdue: list[str] = field(default_factory=list)
    churn_risk: list[str] = field(default_factory=list)
    revenue: float = 0.0
    attribution: dict[str, float] = field(default_factory=dict)
    open_handoffs: int = 0
    lessons_today: int = 0

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "conversations": self.conversations,
            "first_response_p50": self.first_response_p50,
            "first_response_p95": self.first_response_p95,
            "trials_booked": self.trials_booked,
            "trials_attended": self.trials_attended,
            "trials_no_show": self.trials_no_show,
            "paid_after_trial": self.paid_after_trial,
            "enrollments": self.enrollments,
            "groups": self.groups,
            "debt_students": self.debt_students,
            "debt_total": self.debt_total,
            "churn_risk": self.churn_risk,
            "revenue": self.revenue,
            "attribution": self.attribution,
            "open_handoffs": self.open_handoffs,
            "lessons_today": self.lessons_today,
        }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 1)


async def build_daily_report(
    session: AsyncSession, ctx: TenantContext, day: date | None = None
) -> DailyReport:
    today_local = local_date(now_utc(), ctx.tz_name)
    day = day or today_local - timedelta(days=1)
    start = combine_local(day, datetime.min.time(), ctx.tz_name)
    end = start + timedelta(days=1)
    report = DailyReport(day=day, debt_currency=ctx.currency)

    rows = (
        await session.execute(
            select(Event).where(
                Event.tenant_id == ctx.id, Event.created_at >= start, Event.created_at < end
            )
        )
    ).scalars()
    response_times: list[float] = []
    conversations: set[uuid.UUID] = set()
    for event in rows:
        if event.type == ev.INBOUND and event.conversation_id:
            conversations.add(event.conversation_id)
        elif event.type == ev.FIRST_RESPONSE:
            seconds = (event.data or {}).get("seconds")
            if isinstance(seconds, (int, float)):
                response_times.append(float(seconds))
        elif event.type == ev.TRIAL_BOOKED:
            report.trials_booked += 1
        elif event.type == ev.TRIAL_ATTENDED:
            report.trials_attended += 1
        elif event.type == ev.TRIAL_NO_SHOW:
            report.trials_no_show += 1
        elif event.type == ev.ENROLLED:
            report.enrollments += 1
        elif event.type == ev.INVOICE_PAID and event.source == "trial_followup":
            report.paid_after_trial += 1

    report.conversations = len(conversations)
    report.first_response_p50 = _percentile(response_times, 50)
    report.first_response_p95 = _percentile(response_times, 95)

    # Group fill — the number an owner looks at first.
    groups = (
        await session.execute(
            select(Group, Course)
            .join(Course, Course.id == Group.course_id)
            .where(Group.tenant_id == ctx.id, Group.status.in_(["recruiting", "running"]))
            .order_by(Group.name)
        )
    ).all()
    for group, course in groups:
        taken = await en.seats_taken(session, ctx.id, group.id)
        report.groups.append(
            {
                "name": group.name,
                "course": course.label("ru"),
                "taken": taken,
                "capacity": group.capacity,
                "status": group.status,
                "min_to_start": group.min_to_start,
                "short": max(0, group.min_to_start - taken),
            }
        )

    debt = await bl.debt_summary(session, ctx)
    report.debt_students = debt["students"]
    report.debt_total = debt["total"]
    report.debt_currency = debt["currency"]
    report.debt_long_overdue = [
        f"{item['name']} ({item['days']} дн.)" for item in debt["long_overdue"][:5]
    ]

    risk = await att.at_risk_students(session, ctx)
    report.churn_risk = [
        f"{item['name']} ({item['consecutive_absences']} подряд)"
        if item["consecutive_absences"] >= att.CONSECUTIVE_ABSENCES
        else f"{item['name']} ({item['absences_30d']} за месяц)"
        for item in risk[:5]
    ]

    payments = (
        await session.execute(
            select(Payment.attributed_to, func.sum(Payment.amount))
            .where(
                Payment.tenant_id == ctx.id,
                Payment.status == "paid",
                Payment.paid_at >= start,
                Payment.paid_at < end,
            )
            .group_by(Payment.attributed_to)
        )
    ).all()
    for source, total in payments:
        amount = float(total or 0)
        report.revenue += amount
        report.attribution[source] = amount

    report.open_handoffs = (
        await session.execute(
            select(func.count())
            .select_from(Handoff)
            .where(Handoff.tenant_id == ctx.id, Handoff.resolved_at.is_(None))
        )
    ).scalar_one()

    today_start = combine_local(today_local, datetime.min.time(), ctx.tz_name)
    report.lessons_today = (
        await session.execute(
            select(func.count())
            .select_from(Lesson)
            .where(
                Lesson.tenant_id == ctx.id,
                Lesson.starts_at >= today_start,
                Lesson.starts_at < today_start + timedelta(days=1),
                Lesson.status != "cancelled",
            )
        )
    ).scalar_one()

    await session.execute(
        pg_insert(DailyMetric)
        .values(id=uuid.uuid4(), tenant_id=ctx.id, day=day, data=report.as_dict())
        .on_conflict_do_update(
            index_elements=[DailyMetric.tenant_id, DailyMetric.day],
            set_={"data": report.as_dict()},
        )
    )
    return report


def render_daily_report(ctx: TenantContext, report: DailyReport) -> str:
    cur = {"KZT": "₸", "UZS": "сум", "RUB": "₽"}.get(report.debt_currency, "")
    lines = [f"Вчера, {report.day.day} {MONTHS_RU[report.day.month - 1]} · {ctx.name}"]

    if report.first_response_p50 is not None:
        lines.append(
            f"Заявок {report.conversations} · ответ за {report.first_response_p50:.0f} сек "
            f"(95% — {report.first_response_p95:.0f} сек)"
        )
    else:
        lines.append(f"Заявок {report.conversations}")

    if report.trials_booked or report.trials_attended:
        conversion = (
            f" · оплатили {report.paid_after_trial} "
            f"({round(report.paid_after_trial / report.trials_attended * 100)}%)"
            if report.trials_attended
            else ""
        )
        lines.append(
            f"Пробных назначено {report.trials_booked} · пришли {report.trials_attended}"
            + (f" · неявок {report.trials_no_show}" if report.trials_no_show else "")
            + conversion
        )
    if report.enrollments:
        lines.append(f"Зачислений {report.enrollments}")

    if report.groups:
        parts = []
        for group in report.groups[:6]:
            tail = " (не набрана)" if group["short"] > 0 else ""
            parts.append(f"{group['name']} {group['taken']}/{group['capacity']}{tail}")
        lines.append("Группы: " + ", ".join(parts))

    if report.debt_students:
        tail = (
            " · дольше 10 дней: " + ", ".join(report.debt_long_overdue)
            if report.debt_long_overdue
            else ""
        )
        lines.append(
            f"Долги: {report.debt_students} учеников · "
            f"{_money(report.debt_total)} {cur}{tail}"
        )
    if report.churn_risk:
        lines.append("Риск ухода: " + ", ".join(report.churn_risk))
    if report.revenue:
        attrib = ", ".join(
            f"{_source_ru(source)} {_money(amount)}"
            for source, amount in report.attribution.items()
        )
        lines.append(f"Оплаты: {_money(report.revenue)} {cur} · {attrib}")
    if report.lessons_today:
        lines.append(f"Сегодня занятий: {report.lessons_today}")
    if report.open_handoffs:
        lines.append(f"Открытых передач: {report.open_handoffs}")
    return "\n".join(lines)


async def teacher_day_text(
    session: AsyncSession, ctx: TenantContext, teacher_id: uuid.UUID, day: date
) -> str:
    """Today's lessons for the teacher bot."""
    start = combine_local(day, datetime.min.time(), ctx.tz_name)
    rows = (
        await session.execute(
            select(Lesson, Group, Course)
            .join(Group, Group.id == Lesson.group_id)
            .join(Course, Course.id == Group.course_id)
            .where(
                Lesson.tenant_id == ctx.id,
                Group.teacher_id == teacher_id,
                Lesson.starts_at >= start,
                Lesson.starts_at < start + timedelta(days=1),
                Lesson.status != "cancelled",
            )
            .order_by(Lesson.starts_at)
        )
    ).all()
    if not rows:
        return f"На {day:%d.%m} занятий нет."
    lines = [f"Ваши занятия {day:%d.%m}"]
    for lesson, group, course in rows:
        mark = "✅" if lesson.status == "done" else "•"
        students = await en.group_students(session, ctx.id, group.id)
        lines.append(
            f"{mark} {to_tenant(lesson.starts_at, ctx.tz_name):%H:%M} "
            f"{group.name} ({course.label('ru')}) — {len(students)} учеников"
        )
    return "\n".join(lines)


async def group_roster_text(
    session: AsyncSession, ctx: TenantContext, group_id: uuid.UUID
) -> str:
    students = await en.group_students(session, ctx.id, group_id)
    if not students:
        return "В группе пока нет учеников."
    return "\n".join(f"• {student.name}" for student in students)


async def trials_summary(
    session: AsyncSession, ctx: TenantContext, *, days: int = 7
) -> dict:
    since = now_utc() - timedelta(days=days)
    rows = (
        await session.execute(
            select(TrialBooking.status, func.count())
            .join(TrialLesson, TrialLesson.id == TrialBooking.trial_id)
            .where(TrialBooking.tenant_id == ctx.id, TrialLesson.starts_at >= since)
            .group_by(TrialBooking.status)
        )
    ).all()
    counts = {status: count for status, count in rows}
    attended = counts.get("attended", 0)
    return {
        "counts": counts,
        "attendance_rate": (
            round(attended / max(attended + counts.get("no_show", 0), 1), 3)
            if attended or counts.get("no_show")
            else None
        ),
    }


def _money(value: float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _source_ru(source: str) -> str:
    return {
        "miniapp": "Mini App",
        "trial_followup": "дожим после пробного",
        "followup": "дожим",
        "reactivation": "реактивация",
        "referral": "рефералы",
        "bot": "бот",
        "bot_night": "бот ночью",
        "admin": "админ",
        "organic": "прочее",
    }.get(source, source)


def short_name(student: Student) -> str:
    parts = (student.name or "").split()
    if len(parts) <= 1:
        return student.name or "без имени"
    return f"{parts[0]} {parts[1][0]}."


__all__ = [
    "DailyReport",
    "build_daily_report",
    "group_roster_text",
    "render_daily_report",
    "short_name",
    "teacher_day_text",
    "trials_summary",
]
