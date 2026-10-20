"""Staff bot for an education centre: owner, admin and teacher.

A staff member is recognised by ``staff.tg_user_id``. Patient dialogues stay
on the centre's chat; staff write to the bot in their own private chat.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import InboundMessage
from arix.core.context import TenantContext
from arix.core.timeutil import local_date, now_utc, to_tenant
from arix.db.models_core import Staff
from arix.domain.handoff import handoff_card, open_handoffs
from arix.domain.notify import notify_one
from arix.logging_setup import get_logger
from arix.scheduler import jobs
from arix.seed.provision import publish_config
from arix.verticals.edu import attendance as att
from arix.verticals.edu import billing as bl
from arix.verticals.edu.adapter import EduVertical
from arix.verticals.edu.models import Group, Lesson, Teacher, TrialBooking, TrialLesson
from arix.verticals.edu.reports import build_daily_report, render_daily_report

log = get_logger("arix.bots.staff")

HELP_ADMIN = """Команды администратора:
/today — занятия и пробные на сегодня
/groups — наполненность групп
/trials — ближайшие пробные
/handoffs — открытые передачи
/pause_bot, /resume_bot
/help"""

HELP_OWNER = """Команды владельца:
/today — что сегодня
/debts — кто должен
/groups — группы
/trials — пробные
/risk — риск ухода
/report — отчёт за вчера
/pause_bot, /resume_bot
/help"""

HELP_TEACHER = """Команды преподавателя:
/today — ваши занятия сегодня
/lessons — ближайшие уроки
Кнопки под уроком: кто не пришёл, и вердикт пробного.
/cancel_lesson <id> — отменить урок и предупредить группу"""


async def find_staff(
    session: AsyncSession, ctx: TenantContext, tg_user_id: int | None
) -> Staff | None:
    if not tg_user_id:
        return None
    return (
        await session.execute(
            select(Staff).where(
                Staff.tenant_id == ctx.id,
                Staff.tg_user_id == tg_user_id,
                Staff.active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def handle_staff_update(
    session: AsyncSession, ctx: TenantContext, inbound: InboundMessage
) -> bool:
    if inbound.business_connection_id:
        return False
    user_id = int(inbound.from_user_id) if (inbound.from_user_id or "").isdigit() else None
    member = await find_staff(session, ctx, user_id)
    if member is None:
        return False

    if inbound.callback_data:
        await _handle_callback(session, ctx, member, inbound.callback_data)
        return True

    text = (inbound.text or "").strip()
    if not text.startswith("/"):
        await notify_one(session, ctx, member.tg_user_id, "Я понимаю только команды. /help — список.")
        return True

    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    reply, buttons = await _run_command(session, ctx, member, command, argument.strip())
    if reply:
        await notify_one(session, ctx, member.tg_user_id, reply, buttons)
    return True


async def _run_command(
    session: AsyncSession,
    ctx: TenantContext,
    member: Staff,
    command: str,
    argument: str,
) -> tuple[str | None, list[tuple[str, str]] | None]:
    role = member.role
    today = local_date(now_utc(), ctx.tz_name)

    if command in ("/start", "/help"):
        text = {"owner": HELP_OWNER, "admin": HELP_ADMIN, "teacher": HELP_TEACHER}.get(
            role, HELP_ADMIN
        )
        return text, None

    if command == "/today":
        if role == "teacher":
            return await _teacher_today(session, ctx, member), None
        return await _centre_today(session, ctx, today), None

    if command == "/lessons" and role == "teacher":
        return await _teacher_today(session, ctx, member), None

    if command == "/debts":
        if role not in ("owner", "admin"):
            return "Список долгов видит владелец и администратор.", None
        return await _debts(session, ctx), None

    if command == "/groups":
        return await _groups(session, ctx), None

    if command == "/trials":
        return await _trials(session, ctx), None

    if command == "/risk":
        if role not in ("owner", "admin"):
            return "Риск ухода видит владелец и администратор.", None
        return await _risk(session, ctx), None

    if command == "/report":
        report = await build_daily_report(session, ctx)
        return render_daily_report(ctx, report), None

    if command == "/handoffs":
        rows = await open_handoffs(session, ctx.id, limit=10)
        if not rows:
            return "Открытых передач нет.", None
        blocks = []
        for handoff in rows:
            card, _ = await handoff_card(session, ctx, handoff)
            blocks.append(card)
        return "\n\n".join(blocks), None

    if command in ("/pause_bot", "/resume_bot"):
        if role not in ("owner", "admin"):
            return "Эта команда доступна владельцу и администратору.", None
        paused = command == "/pause_bot"
        await publish_config(
            session, ctx.id, {"agent": {"paused": paused}}, note=f"{role}:{command}"
        )
        return (
            "Бот остановлен. Диалоги будут приходить вам." if paused else "Бот снова отвечает."
        ), None

    if command == "/cancel_lesson":
        if role not in ("teacher", "admin", "owner"):
            return "Отменять урок может преподаватель или администратор.", None
        return await _cancel_lesson(session, ctx, argument), None

    return "Не знаю такую команду. /help", None


async def _handle_callback(
    session: AsyncSession, ctx: TenantContext, member: Staff, data: str
) -> None:
    vertical = EduVertical()
    parts = data.split(":")
    outcome = None
    if data.startswith("tb:"):
        outcome = await vertical._teacher_verdict(session, ctx, parts)
    elif data.startswith("at:"):
        outcome = await vertical._attendance_button(session, ctx, parts)
    if outcome and outcome.reply and member.tg_user_id:
        await notify_one(session, ctx, member.tg_user_id, outcome.reply)


async def _teacher_today(session: AsyncSession, ctx: TenantContext, member: Staff) -> str:
    teacher = None
    if member.entity_id:
        teacher = (
            await session.execute(select(Teacher).where(Teacher.id == member.entity_id))
        ).scalar_one_or_none()
    today = local_date(now_utc(), ctx.tz_name)
    start = now_utc() - timedelta(hours=12)
    end = now_utc() + timedelta(hours=36)
    stmt = (
        select(Lesson, Group)
        .join(Group, Group.id == Lesson.group_id)
        .where(
            Lesson.tenant_id == ctx.id,
            Lesson.starts_at >= start,
            Lesson.starts_at <= end,
            Lesson.status == "planned",
        )
        .order_by(Lesson.starts_at)
    )
    if teacher is not None:
        stmt = stmt.where(Group.teacher_id == teacher.id)
    rows = (await session.execute(stmt.limit(12))).all()
    if not rows:
        return f"На {today:%d.%m} занятий не вижу."
    lines = [f"Занятия около {today:%d.%m}:"]
    for lesson, group in rows:
        when = to_tenant(lesson.starts_at, ctx.tz_name)
        lines.append(f"• {when:%d.%m %H:%M} {group.name} — /cancel_lesson {lesson.id}")
    return "\n".join(lines)


async def _centre_today(session: AsyncSession, ctx: TenantContext, today) -> str:
    lessons = (
        await session.execute(
            select(Lesson)
            .where(Lesson.tenant_id == ctx.id, Lesson.status == "planned")
            .order_by(Lesson.starts_at)
            .limit(8)
        )
    ).scalars()
    lines = [f"Сегодня {today:%d.%m}"]
    for lesson in lessons:
        lines.append(f"• урок {to_tenant(lesson.starts_at, ctx.tz_name):%H:%M}")
    debts = await bl.debt_summary(session, ctx)
    lines.append(f"Должников: {debts['students']}, сумма {int(debts['total'])} {debts['currency']}")
    return "\n".join(lines)


async def _debts(session: AsyncSession, ctx: TenantContext) -> str:
    summary = await bl.debt_summary(session, ctx)
    if not summary["students"]:
        return "Открытых счетов нет."
    lines = [f"Должников {summary['students']}, всего {int(summary['total'])} {summary['currency']}"]
    for item in summary["long_overdue"][:15]:
        lines.append(f"• {item['name']}: {int(item['amount'])} · {item['days']} дн.")
    if len(lines) == 1:
        lines.append("Просрочки больше 10 дней нет.")
    return "\n".join(lines)


async def _groups(session: AsyncSession, ctx: TenantContext) -> str:
    from arix.verticals.edu.enrollment import seats_taken

    rows = (
        await session.execute(select(Group).where(Group.tenant_id == ctx.id).order_by(Group.name))
    ).scalars()
    lines = []
    for group in rows:
        taken = await seats_taken(session, ctx.id, group.id)
        tail = "" if taken >= group.min_to_start else " · не набрана"
        lines.append(f"• {group.name}: {taken}/{group.capacity}{tail} ({group.status})")
    return "\n".join(lines) or "Групп ещё нет."


async def _trials(session: AsyncSession, ctx: TenantContext) -> str:
    rows = (
        await session.execute(
            select(TrialLesson, TrialBooking)
            .join(TrialBooking, TrialBooking.trial_id == TrialLesson.id)
            .where(
                TrialLesson.tenant_id == ctx.id,
                TrialBooking.status.in_(["booked", "confirmed"]),
                TrialLesson.starts_at >= now_utc() - timedelta(hours=2),
            )
            .order_by(TrialLesson.starts_at)
            .limit(15)
        )
    ).all()
    if not rows:
        return "Ближайших пробных нет."
    lines = []
    for trial, _booking in rows:
        lines.append(f"• {to_tenant(trial.starts_at, ctx.tz_name):%d.%m %H:%M}")
    return "\n".join(lines)


async def _risk(session: AsyncSession, ctx: TenantContext) -> str:
    rows = await att.at_risk_students(session, ctx)
    if not rows:
        return "Сейчас никто не отмечен как риск ухода."
    return "\n".join(f"• {row.get('name', 'ученик')}" for row in rows[:20])


async def _cancel_lesson(session: AsyncSession, ctx: TenantContext, argument: str) -> str:
    try:
        lesson_id = uuid.UUID(argument.split()[0])
    except (ValueError, IndexError):
        return "Формат: /cancel_lesson <id урока>"
    pair = await att.cancel_lesson(session, ctx, lesson_id, reason="staff")
    if pair is None:
        return "Урок не найден."
    await jobs.enqueue(
        session, ctx.id, "lesson_cancelled_notice", now_utc(),
        dedupe_key=jobs.dedupe("lesson_cancelled", lesson_id),
        payload={"lesson_id": str(lesson_id), "reason": "staff"},
        timezone=ctx.tz_name, respect_quiet_hours=False,
    )
    return f"Урок группы «{pair[1].name}» отменён. Сообщение уйдёт ученикам."


__all__ = ["find_staff", "handle_staff_update"]
