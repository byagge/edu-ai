"""Attendance and churn risk (ТЗ B5 У7).

The teacher marks only who is *missing* — everyone else is present. That is one
tap for a normal lesson, which is the difference between a feature that gets
used and one that does not.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import ABSENCE, CHURN_RISK, log_event
from arix.core.timeutil import local_date, now_utc

from .models import Attendance, Group, Lesson, Student

#: Flags from ТЗ B5 У7.
CONSECUTIVE_ABSENCES = 2
MONTHLY_ABSENCES = 3


async def lesson_with_group(
    session: AsyncSession, tenant_id: uuid.UUID, lesson_id: uuid.UUID
) -> tuple[Lesson, Group] | None:
    row = (
        await session.execute(
            select(Lesson, Group)
            .join(Group, Group.id == Lesson.group_id)
            .where(Lesson.tenant_id == tenant_id, Lesson.id == lesson_id)
        )
    ).first()
    return tuple(row) if row else None  # type: ignore[return-value]


async def mark_attendance(
    session: AsyncSession,
    ctx: TenantContext,
    lesson_id: uuid.UUID,
    absent_student_ids: list[uuid.UUID],
    *,
    late_student_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Record one lesson's attendance. Everyone not listed is present."""
    from .enrollment import group_students

    pair = await lesson_with_group(session, ctx.id, lesson_id)
    if pair is None:
        return {"error": "unknown_lesson"}
    lesson, group = pair

    absent = set(absent_student_ids or [])
    late = set(late_student_ids or []) - absent
    students = await group_students(session, ctx.id, group.id)

    for student in students:
        status = "absent" if student.id in absent else ("late" if student.id in late else "present")
        await session.execute(
            pg_insert(Attendance)
            .values(
                id=uuid.uuid4(),
                tenant_id=ctx.id,
                lesson_id=lesson_id,
                student_id=student.id,
                status=status,
            )
            .on_conflict_do_update(
                index_elements=[Attendance.lesson_id, Attendance.student_id],
                set_={"status": status},
            )
        )

    lesson.status = "done"
    lesson.attendance_marked_at = now_utc()
    await session.flush()

    for student_id in absent:
        await log_event(
            session, ctx.id, ABSENCE, entity_ref=str(lesson_id),
            data={"student_id": str(student_id), "group": group.name},
        )
    return {
        "marked": len(students),
        "absent": len(absent),
        "late": len(late),
        "group": group.name,
    }


async def pending_notices(
    session: AsyncSession, ctx: TenantContext, lesson_id: uuid.UUID
) -> list[tuple[Student, Attendance]]:
    """Absences whose guardian has not been told yet."""
    rows = (
        await session.execute(
            select(Student, Attendance)
            .join(Attendance, Attendance.student_id == Student.id)
            .where(
                Attendance.tenant_id == ctx.id,
                Attendance.lesson_id == lesson_id,
                Attendance.status == "absent",
                Attendance.notified_at.is_(None),
            )
        )
    ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


async def absence_stats(
    session: AsyncSession, ctx: TenantContext, student_id: uuid.UUID
) -> dict:
    """Consecutive and monthly absences — the churn-risk inputs."""
    recent = (
        await session.execute(
            select(Attendance.status, Lesson.starts_at)
            .join(Lesson, Lesson.id == Attendance.lesson_id)
            .where(
                Attendance.tenant_id == ctx.id,
                Attendance.student_id == student_id,
                Lesson.status == "done",
            )
            .order_by(Lesson.starts_at.desc())
            .limit(20)
        )
    ).all()

    consecutive = 0
    for status, _starts_at in recent:
        if status == "absent":
            consecutive += 1
        else:
            break

    month_ago = now_utc() - timedelta(days=30)
    monthly = (
        await session.execute(
            select(func.count())
            .select_from(Attendance)
            .join(Lesson, Lesson.id == Attendance.lesson_id)
            .where(
                Attendance.tenant_id == ctx.id,
                Attendance.student_id == student_id,
                Attendance.status == "absent",
                Lesson.starts_at >= month_ago,
            )
        )
    ).scalar_one()

    return {
        "consecutive_absences": consecutive,
        "absences_30d": int(monthly),
        "at_risk": consecutive >= CONSECUTIVE_ABSENCES or monthly >= MONTHLY_ABSENCES,
    }


async def flag_churn_risk(
    session: AsyncSession, ctx: TenantContext, student_id: uuid.UUID
) -> dict | None:
    """Mark a learner as at risk, at most once per week."""
    stats = await absence_stats(session, ctx, student_id)
    if not stats["at_risk"]:
        return None
    student = (
        await session.execute(
            select(Student).where(Student.tenant_id == ctx.id, Student.id == student_id)
        )
    ).scalar_one_or_none()
    if student is None:
        return None
    if student.churn_risk_at and (now_utc() - student.churn_risk_at) < timedelta(days=7):
        return None
    student.churn_risk_at = now_utc()
    await session.flush()
    await log_event(
        session, ctx.id, CHURN_RISK, contact_id=student.contact_id,
        entity_ref=str(student.id), data=stats | {"name": student.name},
    )
    return stats | {"name": student.name, "student_id": str(student.id)}


async def at_risk_students(session: AsyncSession, ctx: TenantContext) -> list[dict]:
    """For the owner report: who is drifting away."""
    since = now_utc() - timedelta(days=14)
    rows = list(
        (
            await session.execute(
                select(Student).where(
                    Student.tenant_id == ctx.id,
                    Student.status == "active",
                    Student.churn_risk_at.isnot(None),
                    Student.churn_risk_at >= since,
                )
            )
        ).scalars()
    )
    out: list[dict] = []
    for student in rows:
        stats = await absence_stats(session, ctx, student.id)
        out.append({"name": student.name, "student_id": str(student.id), **stats})
    return out


async def lessons_due_attendance(
    session: AsyncSession, ctx: TenantContext, *, within_minutes: int = 15
) -> list[tuple[Lesson, Group]]:
    """Lessons ending about now whose teacher should be prompted."""
    now = now_utc()
    rows = (
        await session.execute(
            select(Lesson, Group)
            .join(Group, Group.id == Lesson.group_id)
            .where(
                Lesson.tenant_id == ctx.id,
                Lesson.status == "planned",
                Lesson.starts_at <= now + timedelta(minutes=within_minutes),
                Lesson.starts_at >= now - timedelta(hours=6),
            )
            .order_by(Lesson.starts_at)
        )
    ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


async def group_attendance_report(
    session: AsyncSession, ctx: TenantContext, group_id: uuid.UUID, *, days: int = 30
) -> dict:
    since = now_utc() - timedelta(days=days)
    rows = (
        await session.execute(
            select(Attendance.status, func.count())
            .join(Lesson, Lesson.id == Attendance.lesson_id)
            .where(
                Attendance.tenant_id == ctx.id,
                Lesson.group_id == group_id,
                Lesson.starts_at >= since,
            )
            .group_by(Attendance.status)
        )
    ).all()
    counts = {status: count for status, count in rows}
    total = sum(counts.values())
    present = counts.get("present", 0) + counts.get("late", 0)
    return {
        "counts": counts,
        "total": total,
        "attendance_rate": round(present / total, 3) if total else None,
    }


async def student_attendance_report(
    session: AsyncSession, ctx: TenantContext, student_id: uuid.UUID, *, days: int = 30
) -> dict:
    since = now_utc() - timedelta(days=days)
    rows = (
        await session.execute(
            select(Attendance.status, func.count())
            .join(Lesson, Lesson.id == Attendance.lesson_id)
            .where(
                Attendance.tenant_id == ctx.id,
                Attendance.student_id == student_id,
                Lesson.starts_at >= since,
            )
            .group_by(Attendance.status)
        )
    ).all()
    counts = {status: count for status, count in rows}
    total = sum(counts.values())
    present = counts.get("present", 0) + counts.get("late", 0)
    return {
        "present": present,
        "absent": counts.get("absent", 0),
        "excused": counts.get("excused", 0),
        "total": total,
        "rate": round(present / total, 3) if total else None,
    }


async def cancel_lesson(
    session: AsyncSession,
    ctx: TenantContext,
    lesson_id: uuid.UUID,
    *,
    reason: str | None = None,
    moved_to: datetime | None = None,
) -> tuple[Lesson, Group] | None:
    pair = await lesson_with_group(session, ctx.id, lesson_id)
    if pair is None:
        return None
    lesson, group = pair
    lesson.status = "moved" if moved_to else "cancelled"
    lesson.cancel_reason = (reason or "")[:255] or None
    if moved_to:
        from .models import Lesson as LessonModel

        session.add(
            LessonModel(
                tenant_id=ctx.id,
                group_id=group.id,
                starts_at=moved_to,
                duration_min=lesson.duration_min,
                status="planned",
                topic=lesson.topic,
            )
        )
    await session.flush()
    return lesson, group


def local_day(ctx: TenantContext, moment: datetime | None = None) -> date:
    return local_date(moment or now_utc(), ctx.tz_name)


__all__ = [
    "CONSECUTIVE_ABSENCES",
    "MONTHLY_ABSENCES",
    "absence_stats",
    "at_risk_students",
    "cancel_lesson",
    "flag_churn_risk",
    "group_attendance_report",
    "lesson_with_group",
    "lessons_due_attendance",
    "mark_attendance",
    "pending_notices",
    "student_attendance_report",
]
