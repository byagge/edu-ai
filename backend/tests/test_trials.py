"""Trial capacity is a lock, not a hope."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from arix.core.context import load_context
from arix.core.timeutil import now_utc
from arix.db.session import tenant_session
from arix.verticals.edu import trials as tr
from arix.verticals.edu.models import Course, Student, TrialBooking, TrialLesson

from .conftest import requires_db

pytestmark = requires_db


async def test_fifty_parallel_bookings_fill_one_seat(tenant):
    async with tenant_session(tenant) as session:
        course = Course(
            tenant_id=tenant, code=f"t-{uuid.uuid4().hex[:6]}",
            name={"ru": "Пробный"}, direction="lang", currency="UZS",
        )
        session.add(course)
        await session.flush()
        trial = TrialLesson(
            tenant_id=tenant, course_id=course.id,
            starts_at=now_utc() + timedelta(days=2), capacity=1,
        )
        session.add(trial)
        students = []
        for _ in range(50):
            student = Student(tenant_id=tenant, name="П", status="lead")
            session.add(student)
            students.append(student)
        await session.flush()
        trial_id = trial.id
        student_ids = [row.id for row in students]

    async def attempt(student_id: uuid.UUID) -> bool:
        async with tenant_session(tenant) as session:
            ctx = await load_context(session, tenant)
            try:
                await tr.book_trial(
                    session, ctx, trial_id=trial_id, student_id=student_id,
                    idempotency_key=f"race:{student_id}",
                )
                return True
            except tr.TrialFull:
                return False

    results = await asyncio.gather(*(attempt(sid) for sid in student_ids))
    assert sum(results) == 1

    async with tenant_session(tenant) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(TrialBooking)
                .where(TrialBooking.tenant_id == tenant, TrialBooking.trial_id == trial_id)
            )
        ).scalar_one()
    assert count == 1
