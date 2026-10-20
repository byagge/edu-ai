"""Invoices and the debt ladder (ТЗ B5 У6).

The arithmetic is pure and lives at the top of this module so it can be tested
exhaustively without a database: a mid-month start, a discount, a pause and a
course that ends mid-period all change the amount, and getting any of them
wrong means asking a parent for the wrong money.
"""
from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import INVOICE_CREATED, log_event
from arix.core.timeutil import combine_local, local_date, now_utc
from arix.db.models_core import Contact

from .models import Enrollment, Group, Invoice, Student

CENTS = Decimal("0.01")


# ---------------------------------------------------------------------------
# Pure arithmetic
# ---------------------------------------------------------------------------


def period_of(day: date) -> str:
    return day.strftime("%Y-%m")


def period_bounds(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def net_price(price_month: Decimal | float, discount_pct: int) -> Decimal:
    price = Decimal(str(price_month))
    if discount_pct:
        price = price * (Decimal(100) - Decimal(discount_pct)) / Decimal(100)
    return price.quantize(CENTS, rounding=ROUND_HALF_UP)


def prorate(
    price_month: Decimal | float,
    discount_pct: int,
    period: str,
    *,
    start_date: date,
    end_date: date | None = None,
    mode: str = "monthly",
) -> tuple[Decimal, str | None]:
    """Amount due for ``period``, plus a note when it is not a full month.

    A learner who joins on the 20th pays for the days that are left, not for
    the whole month — charging the full price is the fastest way to lose a
    parent in week one.  ``mode='course'`` bills the whole course once, so the
    first period carries the full price and later periods are zero.
    """
    first, last = period_bounds(period)
    total_days = (last - first).days + 1

    active_from = max(first, start_date)
    active_to = min(last, end_date) if end_date else last
    if active_to < active_from:
        return Decimal("0.00"), "вне периода обучения"

    full = net_price(price_month, discount_pct)
    if mode == "course":
        if start_date >= first and start_date <= last:
            return full, "оплата за курс целиком"
        return Decimal("0.00"), "курс оплачен ранее"

    active_days = (active_to - active_from).days + 1
    if active_days >= total_days:
        return full, None

    amount = (full * Decimal(active_days) / Decimal(total_days)).quantize(
        CENTS, rounding=ROUND_HALF_UP
    )
    return amount, f"часть месяца: {active_days} из {total_days} дней"


def due_date_for(period: str, billing_day: int, *, start_date: date) -> date:
    """When the invoice must be paid.

    ``billing_day=0`` means "by the start date" (ТЗ: ``by_start``): the day of
    the month the learner started.  Any other value is a fixed day of month,
    clamped to the length of the month.
    """
    first, last = period_bounds(period)
    day = billing_day or start_date.day
    day = min(max(day, 1), last.day)
    candidate = date(first.year, first.month, day)
    return max(candidate, start_date) if start_date > first else candidate


@dataclass(frozen=True)
class LadderStep:
    offset_days: int
    kind: str  # "reminder" | "handoff"

    @property
    def is_handoff(self) -> bool:
        return self.kind == "handoff"


def build_ladder(ladder_days: list[int]) -> list[LadderStep]:
    """Turn ``[-3, 0, 3, 7, 10]`` into steps; the last one escalates to a human."""
    ordered = sorted(set(int(day) for day in ladder_days))
    if not ordered:
        return []
    return [
        LadderStep(day, "handoff" if day == ordered[-1] and day > 0 else "reminder")
        for day in ordered
    ]


def ladder_runs(
    due: date, ladder_days: list[int], *, timezone: str, send_at_hour: int = 10
) -> list[tuple[LadderStep, datetime]]:
    """Absolute UTC instants for every ladder step of one invoice."""
    from datetime import time as _time

    out: list[tuple[LadderStep, datetime]] = []
    for step in build_ladder(ladder_days):
        day = due + timedelta(days=step.offset_days)
        out.append((step, combine_local(day, _time(send_at_hour, 0), timezone)))
    return out


def debt_age_days(due: date, today: date) -> int:
    return max(0, (today - due).days)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


async def create_invoice(
    session: AsyncSession,
    ctx: TenantContext,
    enrollment: Enrollment,
    period: str,
    *,
    group: Group | None = None,
) -> Invoice | None:
    """Issue the invoice for one period. Idempotent per (enrolment, period)."""
    mode = ctx.settings.billing.mode
    amount, note = prorate(
        enrollment.price_month,
        enrollment.discount_pct,
        period,
        start_date=enrollment.start_date,
        end_date=enrollment.end_date,
        mode=mode,
    )
    if amount <= 0:
        return None

    invoice = Invoice(
        tenant_id=ctx.id,
        enrollment_id=enrollment.id,
        student_id=enrollment.student_id,
        period=period,
        amount=amount,
        currency=(group.course.currency if group and group.course else ctx.currency),
        due_date=due_date_for(
            period, enrollment.billing_day, start_date=enrollment.start_date
        ),
        status="open",
        note=note,
    )
    try:
        async with session.begin_nested():
            session.add(invoice)
            await session.flush()
    except IntegrityError:
        # Already issued for this period — the generator ran twice.
        return None

    await log_event(
        session, ctx.id, INVOICE_CREATED,
        entity_ref=str(invoice.id), amount=invoice.amount, currency=invoice.currency,
        data={"period": period, "student_id": str(enrollment.student_id), "note": note},
    )
    return invoice


async def mark_invoice_paid(
    session: AsyncSession,
    ctx: TenantContext,
    invoice_id: uuid.UUID,
    *,
    payment_id: uuid.UUID | None = None,
) -> Invoice | None:
    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.tenant_id == ctx.id, Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if invoice is None or invoice.status == "paid":
        return invoice
    invoice.status = "paid"
    invoice.paid_at = now_utc()
    invoice.payment_id = payment_id
    await session.flush()
    return invoice


async def open_invoices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    student_id: uuid.UUID | None = None,
    overdue_only: bool = False,
    today: date | None = None,
) -> list[Invoice]:
    stmt = select(Invoice).where(
        Invoice.tenant_id == tenant_id, Invoice.status.in_(["open", "overdue"])
    )
    if student_id:
        stmt = stmt.where(Invoice.student_id == student_id)
    if overdue_only:
        stmt = stmt.where(Invoice.due_date < (today or date.today()))
    return list((await session.execute(stmt.order_by(Invoice.due_date))).scalars())


async def refresh_overdue(session: AsyncSession, ctx: TenantContext) -> int:
    """Flip ``open`` invoices past their due date to ``overdue``."""
    today = local_date(now_utc(), ctx.tz_name)
    rows = list(
        (
            await session.execute(
                select(Invoice).where(
                    Invoice.tenant_id == ctx.id,
                    Invoice.status == "open",
                    Invoice.due_date < today,
                )
            )
        ).scalars()
    )
    for invoice in rows:
        invoice.status = "overdue"
    await session.flush()
    return len(rows)


async def debt_summary(session: AsyncSession, ctx: TenantContext) -> dict:
    """Totals for the owner report: how many owe, how much, who is worst."""
    today = local_date(now_utc(), ctx.tz_name)
    rows = (
        await session.execute(
            select(Invoice, Student)
            .join(Student, Student.id == Invoice.student_id)
            .where(Invoice.tenant_id == ctx.id, Invoice.status.in_(["open", "overdue"]))
            .order_by(Invoice.due_date)
        )
    ).all()
    students: dict[uuid.UUID, dict] = {}
    total = Decimal("0.00")
    currency = ctx.currency
    for invoice, student in rows:
        total += invoice.amount
        currency = invoice.currency
        entry = students.setdefault(
            student.id, {"name": student.name, "amount": Decimal("0.00"), "days": 0}
        )
        entry["amount"] += invoice.amount
        entry["days"] = max(entry["days"], debt_age_days(invoice.due_date, today))
    long_overdue = [value for value in students.values() if value["days"] >= 10]
    return {
        "students": len(students),
        "total": float(total),
        "currency": currency,
        "long_overdue": sorted(long_overdue, key=lambda item: -item["days"]),
        "by_student": students,
    }


async def student_balance(
    session: AsyncSession, ctx: TenantContext, student_id: uuid.UUID
) -> dict:
    invoices = await open_invoices(session, ctx.id, student_id=student_id)
    paid_total = (
        await session.execute(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.tenant_id == ctx.id,
                Invoice.student_id == student_id,
                Invoice.status == "paid",
            )
        )
    ).scalar_one()
    today = local_date(now_utc(), ctx.tz_name)
    return {
        "debt": float(sum((invoice.amount for invoice in invoices), Decimal("0.00"))),
        "paid_total": float(paid_total or 0),
        "currency": ctx.currency,
        "invoices": [
            {
                "invoice_id": str(invoice.id),
                "period": invoice.period,
                "amount": float(invoice.amount),
                "due_date": invoice.due_date.isoformat(),
                "status": invoice.status,
                "days_overdue": debt_age_days(invoice.due_date, today),
                "note": invoice.note,
            }
            for invoice in invoices
        ],
    }


async def payer_contacts(
    session: AsyncSession, tenant_id: uuid.UUID, student_id: uuid.UUID
) -> list[Contact]:
    """Who may be told about money: the paying guardian, or the adult learner."""
    from .models import Guardian

    rows = list(
        (
            await session.execute(
                select(Contact)
                .join(Guardian, Guardian.contact_id == Contact.id)
                .where(
                    Guardian.tenant_id == tenant_id,
                    Guardian.student_id == student_id,
                    Guardian.is_payer.is_(True),
                )
            )
        ).scalars()
    )
    if rows:
        return rows
    student = (
        await session.execute(
            select(Student).where(Student.tenant_id == tenant_id, Student.id == student_id)
        )
    ).scalar_one_or_none()
    if student is None or student.contact_id is None:
        return []
    contact = (
        await session.execute(select(Contact).where(Contact.id == student.contact_id))
    ).scalar_one_or_none()
    return [contact] if contact else []


__all__ = [
    "LadderStep",
    "build_ladder",
    "create_invoice",
    "debt_age_days",
    "debt_summary",
    "due_date_for",
    "ladder_runs",
    "mark_invoice_paid",
    "net_price",
    "open_invoices",
    "payer_contacts",
    "period_bounds",
    "period_of",
    "prorate",
    "refresh_overdue",
    "student_balance",
]
