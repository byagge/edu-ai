"""Trial lessons and the follow-up that turns a trial into a payment.

ТЗ B1 names this the main leak: on a hot market only 10–20% of trials convert.
So the teacher's one-tap verdict feeds straight into the message the parent
gets 1–2 hours later, and the ladder stops the moment money arrives.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import TRIAL_BOOKED, log_event
from arix.core.timeutil import humanize_local, local_date, now_utc

from .models import Course, Group, Student, Teacher, TrialBooking, TrialLesson


class TrialFull(Exception):
    """The trial slot has no capacity left."""


@dataclass(frozen=True)
class TrialOffer:
    trial_id: uuid.UUID
    course_id: uuid.UUID | None
    group_id: uuid.UUID | None
    starts_at: datetime
    seats_left: int
    teacher: str | None
    course_name: str | None
    room: str | None

    @property
    def key(self) -> str:
        return str(self.trial_id)


async def booked_count(
    session: AsyncSession, tenant_id: uuid.UUID, trial_id: uuid.UUID
) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(TrialBooking)
            .where(
                TrialBooking.tenant_id == tenant_id,
                TrialBooking.trial_id == trial_id,
                TrialBooking.status.in_(["booked", "confirmed", "attended"]),
            )
        )
    ).scalar_one()


async def find_trials(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    course_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    date_from: date | None = None,
    limit: int = 3,
    now: datetime | None = None,
) -> list[TrialOffer]:
    now = now or now_utc()
    stmt = (
        select(TrialLesson, Course, Teacher)
        .outerjoin(Course, Course.id == TrialLesson.course_id)
        .outerjoin(Teacher, Teacher.id == TrialLesson.teacher_id)
        .where(
            TrialLesson.tenant_id == ctx.id,
            TrialLesson.active.is_(True),
            TrialLesson.starts_at > now + timedelta(minutes=30),
        )
    )
    if course_id:
        stmt = stmt.where(TrialLesson.course_id == course_id)
    if group_id:
        stmt = stmt.where(TrialLesson.group_id == group_id)
    if date_from:
        from arix.core.timeutil import combine_local

        stmt = stmt.where(
            TrialLesson.starts_at >= combine_local(date_from, datetime.min.time(), ctx.tz_name)
        )
    rows = (await session.execute(stmt.order_by(TrialLesson.starts_at).limit(limit * 3))).all()

    offers: list[TrialOffer] = []
    for trial, course, teacher in rows:
        taken = await booked_count(session, ctx.id, trial.id)
        if taken >= trial.capacity:
            continue
        offers.append(
            TrialOffer(
                trial_id=trial.id,
                course_id=trial.course_id,
                group_id=trial.group_id,
                starts_at=trial.starts_at,
                seats_left=trial.capacity - taken,
                teacher=teacher.name if teacher else None,
                course_name=course.label(ctx.default_lang) if course else None,
                room=trial.room,
            )
        )
        if len(offers) >= limit:
            break
    return offers


async def ensure_trial_slots(
    session: AsyncSession,
    ctx: TenantContext,
    course_id: uuid.UUID,
    *,
    weeks: int = 2,
    hour: int = 18,
    weekdays: tuple[int, ...] = (1, 3, 5),
    capacity: int = 5,
) -> int:
    """Create standalone trial slots when the centre has not entered any.

    Without this a brand-new tenant could not offer a trial at all, and the
    first dialogue would end in a handoff.
    """
    from datetime import time as _time

    from arix.core.timeutil import combine_local

    today = local_date(now_utc(), ctx.tz_name)
    created = 0
    for offset in range(1, weeks * 7 + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        starts_at = combine_local(day, _time(hour, 0), ctx.tz_name)
        exists = (
            await session.execute(
                select(TrialLesson.id).where(
                    TrialLesson.tenant_id == ctx.id,
                    TrialLesson.course_id == course_id,
                    TrialLesson.starts_at == starts_at,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            TrialLesson(
                tenant_id=ctx.id,
                course_id=course_id,
                starts_at=starts_at,
                duration_min=60,
                capacity=capacity,
            )
        )
        created += 1
    await session.flush()
    return created


async def book_trial(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    trial_id: uuid.UUID,
    student_id: uuid.UUID,
    attribution: str = "bot",
    idempotency_key: str | None = None,
) -> TrialBooking:
    if idempotency_key:
        existing = (
            await session.execute(
                select(TrialBooking).where(
                    TrialBooking.tenant_id == ctx.id,
                    TrialBooking.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    # Lock the slot row: capacity is a count, so check and insert must not
    # interleave with another parent taking the last seat.
    trial = (
        await session.execute(
            select(TrialLesson)
            .where(TrialLesson.tenant_id == ctx.id, TrialLesson.id == trial_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if trial is None or not trial.active:
        raise ValueError("unknown trial")
    if await booked_count(session, ctx.id, trial_id) >= trial.capacity:
        raise TrialFull("no seats left")

    booking = TrialBooking(
        tenant_id=ctx.id,
        trial_id=trial_id,
        student_id=student_id,
        status="booked",
        attribution=attribution,
        idempotency_key=idempotency_key,
    )
    try:
        async with session.begin_nested():
            session.add(booking)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(TrialBooking).where(
                    TrialBooking.trial_id == trial_id, TrialBooking.student_id == student_id
                )
            )
        ).scalar_one()
        return existing

    student = (
        await session.execute(select(Student).where(Student.id == student_id))
    ).scalar_one()
    if student.status == "lead":
        student.status = "trial"
    await session.flush()

    await log_event(
        session, ctx.id, TRIAL_BOOKED,
        contact_id=student.contact_id,
        entity_ref=str(booking.id),
        source=attribution,
        data={"trial_at": trial.starts_at.isoformat(), "student": student.name},
    )
    return booking


async def set_booking_status(
    session: AsyncSession,
    ctx: TenantContext,
    booking_id: uuid.UUID,
    status: str,
    *,
    feedback_score: int | None = None,
    teacher_note: str | None = None,
) -> TrialBooking | None:
    booking = (
        await session.execute(
            select(TrialBooking).where(
                TrialBooking.tenant_id == ctx.id, TrialBooking.id == booking_id
            )
        )
    ).scalar_one_or_none()
    if booking is None:
        return None
    booking.status = status
    if feedback_score is not None:
        booking.feedback_score = max(1, min(5, int(feedback_score)))
    if teacher_note:
        booking.teacher_note = teacher_note[:2000]
    await session.flush()
    return booking


async def booking_with_refs(
    session: AsyncSession, tenant_id: uuid.UUID, booking_id: uuid.UUID
) -> tuple[TrialBooking, TrialLesson, Student, Course | None] | None:
    row = (
        await session.execute(
            select(TrialBooking, TrialLesson, Student, Course)
            .join(TrialLesson, TrialLesson.id == TrialBooking.trial_id)
            .join(Student, Student.id == TrialBooking.student_id)
            .outerjoin(Course, Course.id == TrialLesson.course_id)
            .where(TrialBooking.tenant_id == tenant_id, TrialBooking.id == booking_id)
        )
    ).first()
    return tuple(row) if row else None  # type: ignore[return-value]


async def upcoming_trials_for_student(
    session: AsyncSession, tenant_id: uuid.UUID, student_id: uuid.UUID
) -> list[tuple[TrialBooking, TrialLesson]]:
    rows = (
        await session.execute(
            select(TrialBooking, TrialLesson)
            .join(TrialLesson, TrialLesson.id == TrialBooking.trial_id)
            .where(
                TrialBooking.tenant_id == tenant_id,
                TrialBooking.student_id == student_id,
                TrialBooking.status.in_(["booked", "confirmed"]),
                TrialLesson.starts_at > now_utc(),
            )
            .order_by(TrialLesson.starts_at)
        )
    ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


async def trials_needing_attendance(
    session: AsyncSession, ctx: TenantContext
) -> list[tuple[TrialLesson, list[TrialBooking]]]:
    """Trials that already happened but have no verdict yet."""
    cutoff = now_utc()
    rows = list(
        (
            await session.execute(
                select(TrialLesson).where(
                    TrialLesson.tenant_id == ctx.id,
                    TrialLesson.starts_at < cutoff,
                    TrialLesson.starts_at > cutoff - timedelta(days=2),
                )
            )
        ).scalars()
    )
    out: list[tuple[TrialLesson, list[TrialBooking]]] = []
    for trial in rows:
        pending = list(
            (
                await session.execute(
                    select(TrialBooking).where(
                        TrialBooking.tenant_id == ctx.id,
                        TrialBooking.trial_id == trial.id,
                        TrialBooking.status.in_(["booked", "confirmed"]),
                    )
                )
            ).scalars()
        )
        if pending:
            out.append((trial, pending))
    return out


def offer_text(offer: TrialOffer, ctx: TenantContext, lang: str = "ru") -> str:
    when = humanize_local(offer.starts_at, ctx.tz_name, lang)
    parts = [when]
    if offer.teacher:
        parts.append(f"преподаватель {offer.teacher}")
    if offer.seats_left <= 2:
        parts.append(f"осталось мест: {offer.seats_left}")
    return ", ".join(parts)


async def recommend_group(
    session: AsyncSession, ctx: TenantContext, booking: TrialBooking
) -> Group | None:
    """Pick the group to offer after a trial: the trial's own, else the course's."""
    from .enrollment import list_groups

    trial = (
        await session.execute(select(TrialLesson).where(TrialLesson.id == booking.trial_id))
    ).scalar_one()
    if booking.recommended_group_id:
        return (
            await session.execute(
                select(Group).where(Group.id == booking.recommended_group_id)
            )
        ).scalar_one_or_none()
    if trial.group_id:
        return (
            await session.execute(select(Group).where(Group.id == trial.group_id))
        ).scalar_one_or_none()
    if trial.course_id:
        offers = await list_groups(session, ctx, course_id=trial.course_id, limit=1)
        if offers:
            return (
                await session.execute(
                    select(Group).where(Group.id == offers[0].group_id)
                )
            ).scalar_one_or_none()
    return None


__all__ = [
    "TrialFull",
    "TrialOffer",
    "book_trial",
    "booked_count",
    "booking_with_refs",
    "ensure_trial_slots",
    "find_trials",
    "offer_text",
    "recommend_group",
    "set_booking_status",
    "trials_needing_attendance",
    "upcoming_trials_for_student",
]
