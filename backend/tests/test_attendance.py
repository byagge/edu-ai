"""Two absences in a row flag churn, and not more than once a week."""
from __future__ import annotations

import uuid
from datetime import timedelta

from arix.core.context import load_context
from arix.core.timeutil import now_utc
from arix.db.session import tenant_session
from arix.verticals.edu import attendance as att
from arix.verticals.edu.models import Course, Enrollment, Group, Lesson, Student

from .conftest import requires_db

pytestmark = requires_db


async def _lesson(session, tenant, group_id, when):
    lesson = Lesson(
        tenant_id=tenant, group_id=group_id, starts_at=when, status="planned", duration_min=60
    )
    session.add(lesson)
    await session.flush()
    return lesson


async def test_two_consecutive_absences_flag_once_per_week(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        course = Course(
            tenant_id=tenant, code=f"a-{uuid.uuid4().hex[:4]}",
            name={"ru": "Курс"}, direction="lang", price_month=10, currency="UZS",
        )
        session.add(course)
        await session.flush()
        group = Group(
            tenant_id=tenant, course_id=course.id, name="G", capacity=5,
            min_to_start=1, price_month=10, status="running",
        )
        session.add(group)
        student = Student(tenant_id=tenant, name="Алия", status="active")
        session.add(student)
        await session.flush()
        session.add(
            Enrollment(
                tenant_id=tenant, student_id=student.id, group_id=group.id,
                price_month=10, start_date=now_utc().date(), status="active",
            )
        )
        first = await _lesson(session, tenant, group.id, now_utc() - timedelta(days=3))
        second = await _lesson(session, tenant, group.id, now_utc() - timedelta(days=1))
        await att.mark_attendance(session, ctx, first.id, [student.id])
        await att.mark_attendance(session, ctx, second.id, [student.id])
        flagged = await att.flag_churn_risk(session, ctx, student.id)
        assert flagged is not None
        assert flagged["consecutive_absences"] >= 2
        again = await att.flag_churn_risk(session, ctx, student.id)
        assert again is None
