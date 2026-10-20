"""A seat in a group is taken at most once, even under a race."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from arix.core.context import load_context
from arix.db.session import tenant_session
from arix.verticals.edu import enrollment as en
from arix.verticals.edu.models import Course, Enrollment, Group, Student

from .conftest import requires_db

pytestmark = requires_db


async def _group(session, tenant, *, capacity: int = 1) -> Group:
    course = Course(
        tenant_id=tenant,
        code=f"c-{uuid.uuid4().hex[:6]}",
        name={"ru": "Тест"},
        direction="lang",
        price_month=100_000,
        currency="UZS",
    )
    session.add(course)
    await session.flush()
    group = Group(
        tenant_id=tenant,
        course_id=course.id,
        name=f"G-{uuid.uuid4().hex[:4]}",
        capacity=capacity,
        min_to_start=1,
        status="recruiting",
        price_month=100_000,
    )
    session.add(group)
    await session.flush()
    return group


async def _student(session, tenant) -> Student:
    student = Student(tenant_id=tenant, name=f"Ученик {uuid.uuid4().hex[:4]}", status="lead")
    session.add(student)
    await session.flush()
    return student


async def test_fifty_parallel_enrolments_take_exactly_one_seat(tenant):
    async with tenant_session(tenant) as session:
        group = await _group(session, tenant, capacity=1)
        students = [await _student(session, tenant) for _ in range(50)]
        group_id = group.id
        student_ids = [row.id for row in students]

    async def attempt(student_id: uuid.UUID) -> str:
        async with tenant_session(tenant) as session:
            ctx = await load_context(session, tenant)
            try:
                await en.enroll(
                    session, ctx,
                    student_id=student_id,
                    group_id=group_id,
                    idempotency_key=f"race:{student_id}",
                )
                return "ok"
            except en.SeatsFull:
                return "full"
            except en.AlreadyEnrolled:
                return "already"

    results = await asyncio.gather(*(attempt(sid) for sid in student_ids))
    assert results.count("ok") == 1, results

    async with tenant_session(tenant) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.tenant_id == tenant, Enrollment.group_id == group_id)
            )
        ).scalar_one()
    assert count == 1


async def test_same_idempotency_key_returns_the_same_enrolment(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        group = await _group(session, tenant, capacity=5)
        student = await _student(session, tenant)
        first, _ = await en.enroll(
            session, ctx, student_id=student.id, group_id=group.id, idempotency_key="same"
        )
        second, _ = await en.enroll(
            session, ctx, student_id=student.id, group_id=group.id, idempotency_key="same"
        )
    assert first.id == second.id


async def test_second_enrolment_of_the_same_student_is_rejected(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        group = await _group(session, tenant, capacity=5)
        student = await _student(session, tenant)
        await en.enroll(session, ctx, student_id=student.id, group_id=group.id)
        with pytest.raises(en.AlreadyEnrolled):
            await en.enroll(session, ctx, student_id=student.id, group_id=group.id)
