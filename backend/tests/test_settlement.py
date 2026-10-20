"""Paying an invoice closes it and cancels the debt ladder."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from arix.core.context import load_context
from arix.core.timeutil import now_utc
from arix.db.models_core import Payment, ScheduledJob
from arix.db.session import tenant_session
from arix.payments import mark_paid
from arix.verticals.edu.models import Course, Enrollment, Group, Invoice, Student
from arix.verticals.edu.scheduling import schedule_invoice_ladder
from arix.verticals.edu.settlement import settle_payment

from .conftest import requires_db

pytestmark = requires_db


async def test_payment_marks_the_invoice_paid_and_cancels_reminders(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        course = Course(
            tenant_id=tenant, code=f"p-{uuid.uuid4().hex[:4]}",
            name={"ru": "Курс"}, direction="lang", price_month=50_000, currency="UZS",
        )
        session.add(course)
        await session.flush()
        group = Group(
            tenant_id=tenant, course_id=course.id, name="G", capacity=5,
            min_to_start=1, price_month=50_000, status="running",
        )
        session.add(group)
        student = Student(tenant_id=tenant, name="Бек", status="active")
        session.add(student)
        await session.flush()
        enrollment = Enrollment(
            tenant_id=tenant, student_id=student.id, group_id=group.id,
            price_month=50_000, start_date=now_utc().date(), status="active",
        )
        session.add(enrollment)
        await session.flush()
        invoice = Invoice(
            tenant_id=tenant, enrollment_id=enrollment.id, student_id=student.id,
            period="2026-10", amount=50_000, currency="UZS",
            due_date=(now_utc() + timedelta(days=5)).date(), status="open",
        )
        session.add(invoice)
        await session.flush()
        armed = await schedule_invoice_ladder(session, ctx, invoice)
        assert armed
        payment = Payment(
            tenant_id=tenant, provider="manual", amount=50_000, currency="UZS",
            status="created", attributed_to="cash", entity_ref=str(invoice.id),
            external_id=f"t:{invoice.id}",
        )
        session.add(payment)
        await session.flush()
        paid = await mark_paid(session, ctx, payment.id, provider_ref="cash")
        assert paid is not None
        closed = await settle_payment(session, ctx, paid)
        assert closed is not None and closed.status == "paid"
        pending = (
            await session.execute(
                select(ScheduledJob).where(
                    ScheduledJob.tenant_id == tenant,
                    ScheduledJob.status == "pending",
                    ScheduledJob.kind.in_(["pay_reminder", "pay_handoff"]),
                )
            )
        ).scalars().all()
        assert pending == []
