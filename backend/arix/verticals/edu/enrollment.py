"""Courses, groups, seats and enrolment.

Capacity is the invariant here, and it cannot be expressed as an exclusion
constraint (a group is a count, not an interval), so every seat is taken inside
``SELECT ... FOR UPDATE`` on the group row.  Two parents clicking the last seat
at the same time therefore serialise, and exactly one wins.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import ENROLLED, log_event
from arix.core.timeutil import local_date, now_utc
from arix.db.models_core import Contact

from .models import Course, Enrollment, Group, Guardian, Student, Teacher

WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

#: Words that carry no signal when matching a course by free text.
STOPWORDS = {
    "сколько", "стоит", "стоят", "цена", "хочу", "можно", "для", "мне", "это", "что",
    "как", "есть", "нужно", "вас", "ваш", "про", "курс", "курсы", "занятия", "записать",
    "ребенка", "ребёнка", "сына", "дочь", "лет", "год", "года", "qancha", "narxi",
}


class SeatsFull(Exception):
    """Raised when the group has no free seat left."""


class AlreadyEnrolled(Exception):
    """Raised when this student already holds a seat in this group."""


@dataclass(frozen=True)
class GroupOffer:
    group_id: uuid.UUID
    course_id: uuid.UUID
    name: str
    course_name: str
    level: str | None
    teacher: str | None
    schedule_text: str
    price_month: float | None
    currency: str
    seats_total: int
    seats_taken: int
    start_date: date | None
    status: str

    @property
    def seats_left(self) -> int:
        return max(0, self.seats_total - self.seats_taken)


def tokens(text: str) -> list[str]:
    words = re.findall(r"[\w\-']+", (text or "").lower(), flags=re.UNICODE)
    return [word for word in words if len(word) > 2 and word not in STOPWORDS]


def schedule_text(schedule: dict | None, lang: str = "ru") -> str:
    """``{"days": [0,2,4], "time": "18:00"}`` → ``«пн, ср, пт в 18:00»``."""
    schedule = schedule or {}
    days = schedule.get("days") or []
    at = schedule.get("time")
    names = ", ".join(WEEKDAYS_RU[int(day) % 7] for day in days) if days else ""
    if names and at:
        return f"{names} в {at}"
    return names or (at or "расписание уточняется")


def parse_age(text: str) -> int | None:
    """Pull an age out of «ребёнку 10 лет» / «мне 25»."""
    match = re.search(r"\b(\d{1,2})\s*(?:лет|года|год|yosh|жас)\b", (text or "").lower())
    if match:
        age = int(match.group(1))
        return age if 2 <= age <= 99 else None
    return None


def _active_courses(tenant_id: uuid.UUID) -> Select:
    return select(Course).where(Course.tenant_id == tenant_id, Course.active.is_(True))


async def list_courses(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    direction: str | None = None,
    query: str | None = None,
    age: int | None = None,
    limit: int = 12,
    with_match_flag: bool = False,
) -> list[Course] | tuple[list[Course], bool]:
    """Active courses, ranked by a free-text goal when one is given.

    ``with_match_flag`` reports whether the query actually matched: answering
    "фортепиано" with the price of the English course would be exactly the
    invented price ТЗ forbids, so the caller hands over instead.
    """
    stmt = _active_courses(tenant_id)
    if direction:
        stmt = stmt.where(Course.direction == direction)
    rows = list((await session.execute(stmt.order_by(Course.sort, Course.code))).scalars())

    if age is not None:
        fitting = [
            course
            for course in rows
            if (course.age_from is None or age >= course.age_from)
            and (course.age_to is None or age <= course.age_to)
        ]
        rows = fitting or rows

    matched = not query
    if query:
        needles = tokens(query)
        if needles:
            def score(course: Course) -> int:
                hay = set(
                    tokens(
                        " ".join(
                            [
                                *(str(value) for value in course.name.values()),
                                course.code,
                                course.direction,
                                course.level_from or "",
                                course.level_to or "",
                                *course.goal_keywords,
                            ]
                        )
                    )
                )
                total = 0
                for needle in needles:
                    if needle in hay:
                        total += 10
                    elif len(needle) >= 4 and any(word.startswith(needle[:4]) for word in hay):
                        total += 4
                return total

            scored = [(score(course), course) for course in rows]
            hits = [course for value, course in sorted(scored, key=lambda p: -p[0]) if value > 0]
            matched = bool(hits)
            rows = hits or rows

    result = rows[:limit]
    return (result, matched) if with_match_flag else result


async def seats_taken(session: AsyncSession, tenant_id: uuid.UUID, group_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Enrollment)
            .where(
                Enrollment.tenant_id == tenant_id,
                Enrollment.group_id == group_id,
                Enrollment.status.in_(["reserved", "active", "paused"]),
            )
        )
    ).scalar_one()


async def list_groups(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    course_id: uuid.UUID | None = None,
    level: str | None = None,
    only_open: bool = True,
    limit: int = 8,
) -> list[GroupOffer]:
    stmt = (
        select(Group, Course, Teacher)
        .join(Course, Course.id == Group.course_id)
        .outerjoin(Teacher, Teacher.id == Group.teacher_id)
        .where(Group.tenant_id == ctx.id)
    )
    if course_id:
        stmt = stmt.where(Group.course_id == course_id)
    if level:
        stmt = stmt.where(Group.level == level)
    if only_open:
        stmt = stmt.where(Group.status.in_(["recruiting", "running"]))
    rows = (await session.execute(stmt.order_by(Group.start_date.nullslast()))).all()

    offers: list[GroupOffer] = []
    for group, course, teacher in rows:
        taken = await seats_taken(session, ctx.id, group.id)
        if only_open and taken >= group.capacity:
            continue
        offers.append(
            GroupOffer(
                group_id=group.id,
                course_id=course.id,
                name=group.name,
                course_name=course.label(ctx.default_lang),
                level=group.level,
                teacher=teacher.name if teacher else None,
                schedule_text=schedule_text(group.schedule),
                price_month=float(group.price_month or course.price_month or 0) or None,
                currency=course.currency,
                seats_total=group.capacity,
                seats_taken=taken,
                start_date=group.start_date,
                status=group.status,
            )
        )
    return offers[:limit]


async def ensure_student(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    name: str,
    contact: Contact,
    birth_year: int | None = None,
    level: str | None = None,
    is_self: bool = False,
) -> Student:
    """Find or create the learner behind a dialogue.

    For a child the contact becomes a guardian rather than the student's own
    contact, so reports and balances can only ever go to the adult (ТЗ B10).
    """
    existing = None
    if is_self:
        existing = (
            await session.execute(
                select(Student).where(
                    Student.tenant_id == ctx.id, Student.contact_id == contact.id
                )
            )
        ).scalars().first()
    if existing is None:
        existing = (
            await session.execute(
                select(Student)
                .join(Guardian, Guardian.student_id == Student.id)
                .where(
                    Student.tenant_id == ctx.id,
                    Guardian.contact_id == contact.id,
                    func.lower(Student.name) == name.strip().lower(),
                )
            )
        ).scalars().first()

    if existing is not None:
        if birth_year and not existing.birth_year:
            existing.birth_year = birth_year
        if level and not existing.level:
            existing.level = level
        await session.flush()
        return existing

    student = Student(
        tenant_id=ctx.id,
        contact_id=contact.id if is_self else None,
        name=name.strip()[:160],
        birth_year=birth_year,
        level=level,
        status="lead",
    )
    session.add(student)
    await session.flush()
    if not is_self:
        session.add(
            Guardian(
                tenant_id=ctx.id,
                contact_id=contact.id,
                student_id=student.id,
                relation="parent",
                is_payer=True,
            )
        )
        await session.flush()
    return student


async def enroll(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    student_id: uuid.UUID,
    group_id: uuid.UUID,
    attribution: str = "bot",
    start_date: date | None = None,
    discount_pct: int = 0,
    idempotency_key: str | None = None,
) -> tuple[Enrollment, object | None]:
    """Reserve a seat and issue the first invoice.

    Returns ``(enrollment, invoice)``.  Raises :class:`SeatsFull` or
    :class:`AlreadyEnrolled` — both are normal outcomes the agent explains.
    """
    from .billing import create_invoice, period_of

    if idempotency_key:
        existing = (
            await session.execute(
                select(Enrollment).where(
                    Enrollment.tenant_id == ctx.id,
                    Enrollment.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, None

    # Lock the group row: capacity is a count, so the check and the insert must
    # not interleave with another dialogue taking the same last seat.
    group = (
        await session.execute(
            select(Group)
            .where(Group.tenant_id == ctx.id, Group.id == group_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if group is None:
        raise ValueError("unknown group")
    if group.status in ("closed", "paused"):
        raise SeatsFull(f"group is {group.status}")
    if await seats_taken(session, ctx.id, group_id) >= group.capacity:
        raise SeatsFull("no seats left")

    course = (
        await session.execute(select(Course).where(Course.id == group.course_id))
    ).scalar_one()
    start = start_date or group.start_date or local_date(now_utc(), ctx.tz_name)
    billing_day = 0 if ctx.settings.billing.billing_day == "by_start" else _fixed_day(
        ctx.settings.billing.billing_day
    )

    enrollment = Enrollment(
        tenant_id=ctx.id,
        student_id=student_id,
        group_id=group_id,
        price_month=group.price_month or course.price_month or 0,
        discount_pct=discount_pct,
        billing_day=billing_day,
        start_date=start,
        status="reserved",
        attribution=attribution,
        idempotency_key=idempotency_key,
    )
    try:
        async with session.begin_nested():
            session.add(enrollment)
            await session.flush()
    except IntegrityError as exc:
        raise AlreadyEnrolled("student already holds a seat in this group") from exc

    student = (
        await session.execute(select(Student).where(Student.id == student_id))
    ).scalar_one()
    student.status = "active"

    # A group that has reached its minimum can be announced as starting.
    if group.status == "recruiting":
        taken = await seats_taken(session, ctx.id, group_id)
        if taken >= group.min_to_start:
            group.status = "running"

    await session.flush()
    invoice = await create_invoice(session, ctx, enrollment, period_of(start), group=group)

    await log_event(
        session, ctx.id, ENROLLED,
        contact_id=student.contact_id,
        entity_ref=str(enrollment.id),
        amount=float(invoice.amount) if invoice else None,
        currency=course.currency,
        source=attribution,
        data={"group": group.name, "course": course.label("ru")},
    )
    return enrollment, invoice


def _fixed_day(spec: str) -> int:
    match = re.search(r"(\d{1,2})", spec or "")
    return min(max(int(match.group(1)), 1), 28) if match else 1


async def activate_enrollment(
    session: AsyncSession, ctx: TenantContext, enrollment_id: uuid.UUID
) -> Enrollment | None:
    enrollment = (
        await session.execute(
            select(Enrollment).where(
                Enrollment.tenant_id == ctx.id, Enrollment.id == enrollment_id
            )
        )
    ).scalar_one_or_none()
    if enrollment is None:
        return None
    enrollment.status = "active"
    await session.flush()
    return enrollment


async def group_students(
    session: AsyncSession, tenant_id: uuid.UUID, group_id: uuid.UUID
) -> list[Student]:
    return list(
        (
            await session.execute(
                select(Student)
                .join(Enrollment, Enrollment.student_id == Student.id)
                .where(
                    Enrollment.tenant_id == tenant_id,
                    Enrollment.group_id == group_id,
                    Enrollment.status.in_(["reserved", "active"]),
                )
                .order_by(Student.name)
            )
        ).scalars()
    )


async def student_groups(
    session: AsyncSession, tenant_id: uuid.UUID, student_id: uuid.UUID
) -> list[tuple[Enrollment, Group, Course]]:
    rows = (
        await session.execute(
            select(Enrollment, Group, Course)
            .join(Group, Group.id == Enrollment.group_id)
            .join(Course, Course.id == Group.course_id)
            .where(
                Enrollment.tenant_id == tenant_id,
                Enrollment.student_id == student_id,
                Enrollment.status.in_(["reserved", "active", "paused"]),
            )
        )
    ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


async def students_of_contact(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID
) -> list[Student]:
    """Every learner this contact may ask about — their own and their children.

    Nothing outside this list is ever disclosed; asking about someone else's
    child is a handoff, not an answer (ТЗ B11).
    """
    own = list(
        (
            await session.execute(
                select(Student).where(
                    Student.tenant_id == tenant_id, Student.contact_id == contact_id
                )
            )
        ).scalars()
    )
    wards = list(
        (
            await session.execute(
                select(Student)
                .join(Guardian, Guardian.student_id == Student.id)
                .where(Guardian.tenant_id == tenant_id, Guardian.contact_id == contact_id)
            )
        ).scalars()
    )
    seen: dict[uuid.UUID, Student] = {}
    for student in [*own, *wards]:
        seen[student.id] = student
    return list(seen.values())


async def recruiting_groups_ready(
    session: AsyncSession, ctx: TenantContext
) -> list[tuple[Group, int]]:
    """Groups that reached ``min_to_start`` and can be announced (ТЗ B5 У5)."""
    rows = list(
        (
            await session.execute(
                select(Group).where(Group.tenant_id == ctx.id, Group.status == "recruiting")
            )
        ).scalars()
    )
    ready: list[tuple[Group, int]] = []
    for group in rows:
        taken = await seats_taken(session, ctx.id, group.id)
        if taken >= group.min_to_start:
            ready.append((group, taken))
    return ready


async def generate_lessons(
    session: AsyncSession,
    ctx: TenantContext,
    group: Group,
    *,
    weeks: int = 4,
    from_date: date | None = None,
) -> int:
    """Materialise the group's timetable into ``lessons`` rows.

    Attendance and absence notices hang off concrete lessons, so the schedule
    has to exist as rows, not only as a JSON pattern.
    """
    from datetime import time as _time

    from arix.core.timeutil import combine_local

    from .models import Lesson

    schedule = group.schedule or {}
    days = [int(day) % 7 for day in (schedule.get("days") or [])]
    at = schedule.get("time")
    if not days or not at:
        return 0
    hour, _, minute = str(at).partition(":")
    lesson_time = _time(int(hour), int(minute or 0))

    course = (
        await session.execute(select(Course).where(Course.id == group.course_id))
    ).scalar_one()
    start = from_date or group.start_date or local_date(now_utc(), ctx.tz_name)
    created = 0
    for offset in range(weeks * 7):
        day = start + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        starts_at = combine_local(day, lesson_time, ctx.tz_name)
        exists = (
            await session.execute(
                select(Lesson.id).where(
                    Lesson.tenant_id == ctx.id,
                    Lesson.group_id == group.id,
                    Lesson.starts_at == starts_at,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            Lesson(
                tenant_id=ctx.id,
                group_id=group.id,
                starts_at=starts_at,
                duration_min=course.lesson_min,
                status="planned",
            )
        )
        created += 1
    await session.flush()
    return created


async def group_with_refs(
    session: AsyncSession, tenant_id: uuid.UUID, group_id: uuid.UUID
) -> tuple[Group, Course, Teacher | None] | None:
    row = (
        await session.execute(
            select(Group, Course, Teacher)
            .join(Course, Course.id == Group.course_id)
            .outerjoin(Teacher, Teacher.id == Group.teacher_id)
            .where(Group.tenant_id == tenant_id, Group.id == group_id)
        )
    ).first()
    return tuple(row) if row else None  # type: ignore[return-value]


def price_text(offer: GroupOffer) -> str:
    if not offer.price_month:
        return "цена уточняется"
    symbol = {"KZT": "₸", "UZS": "сум", "RUB": "₽"}.get(offer.currency, offer.currency)
    return f"{int(offer.price_month):,}".replace(",", " ") + f" {symbol}/мес"


__all__ = [
    "AlreadyEnrolled",
    "GroupOffer",
    "SeatsFull",
    "activate_enrollment",
    "enroll",
    "ensure_student",
    "generate_lessons",
    "group_students",
    "group_with_refs",
    "list_courses",
    "list_groups",
    "parse_age",
    "price_text",
    "recruiting_groups_ready",
    "schedule_text",
    "seats_taken",
    "student_groups",
    "students_of_contact",
    "tokens",
]
