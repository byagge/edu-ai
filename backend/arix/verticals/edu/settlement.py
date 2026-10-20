"""What happens when money arrives.

A payment closes the invoice it points at, stops the debt ladder and stops the
post-trial follow-up.  The same function serves the provider webhook and the
admin's "paid in cash" button, so the two paths cannot drift apart.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import INVOICE_PAID, log_event
from arix.db.models_core import Payment
from arix.scheduler import jobs

from .billing import mark_invoice_paid
from .models import Invoice, TrialBooking
from .scheduling import cancel_invoice_ladder


async def settle_payment(
    session: AsyncSession, ctx: TenantContext, payment: Payment
) -> Invoice | None:
    """Close the invoice named by ``payment.entity_ref``, if there is one."""
    raw = (payment.entity_ref or "").strip()
    try:
        invoice_id = uuid.UUID(raw)
    except ValueError:
        return None

    existing = (
        await session.execute(
            select(Invoice).where(Invoice.tenant_id == ctx.id, Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    was_open = existing.status != "paid"
    invoice = await mark_invoice_paid(
        session, ctx, invoice_id, payment_id=payment.id
    )
    if invoice is None:
        return None

    await cancel_invoice_ladder(session, ctx, invoice.id)
    bookings = (
        await session.execute(
            select(TrialBooking.id).where(
                TrialBooking.tenant_id == ctx.id,
                TrialBooking.student_id == invoice.student_id,
            )
        )
    ).scalars()
    for booking_id in bookings:
        await jobs.cancel(session, ctx.id, dedupe_prefix=f"trial_followup:{booking_id}")

    if was_open:
        await log_event(
            session, ctx.id, INVOICE_PAID,
            contact_id=payment.contact_id,
            entity_ref=str(invoice.id),
            amount=invoice.amount,
            currency=invoice.currency,
            source=payment.attributed_to or "organic",
            data={"payment_id": str(payment.id), "student_id": str(invoice.student_id)},
        )
    return invoice


__all__ = ["settle_payment"]
