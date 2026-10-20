"""Education-centre timers (ТЗ B6)."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.timeutil import combine_local, local_date, now_utc, parse_hhmm, to_tenant
from arix.scheduler import jobs

from . import billing as bl
from . import trials as tr
from .models import Enrollment, Invoice


async def schedule_trial_jobs(
    session: AsyncSession, ctx: TenantContext, booking_id: uuid.UUID
) -> list[str]:
    """Reminders for one trial booking: −24 h and −2 h."""
    refs = await tr.booking_with_refs(session, ctx.id, booking_id)
    if refs is None:
        return []
    booking, trial, _student, _course = refs
    if booking.status in ("cancelled", "attended", "no_show"):
        await cancel_trial_jobs(session, ctx, booking_id)
        return []

    quiet = ctx.settings.quiet
    armed: list[str] = []
    for hours, tag in ((24, "24h"), (2, "2h")):
        run_at = trial.starts_at - timedelta(hours=hours)
        if run_at <= now_utc():
            continue
        respect_quiet = True
        if tag == "2h" and quiet and quiet.contains_local(to_tenant(run_at, ctx.tz_name)):
            # Same reasoning as in the clinic: an early-morning trial must be
            # reminded at 08:00, not pushed to 09:00 — after the lesson.
            candidate = combine_local(
                local_date(trial.starts_at, ctx.tz_name),
                parse_hhmm(ctx.settings.reminders.early_morning_send_at),
                ctx.tz_name,
            )
            if candidate > now_utc():
                run_at = candidate
            respect_quiet = False
        await jobs.enqueue(
            session, ctx.id, "trial_reminder", run_at,
            dedupe_key=jobs.dedupe("trial_reminder", booking_id, tag),
            payload={"booking_id": str(booking_id), "offset": tag},
            timezone=ctx.tz_name, quiet=quiet,
            respect_quiet_hours=respect_quiet, replace=True,
        )
        armed.append(f"trial_reminder:{tag}")

    # Ask the teacher for a verdict shortly after the trial ends.
    await jobs.enqueue(
        session, ctx.id, "trial_verdict_prompt",
        trial.starts_at + timedelta(minutes=trial.duration_min + 10),
        dedupe_key=jobs.dedupe("trial_verdict", booking_id),
        payload={"booking_id": str(booking_id)},
        timezone=ctx.tz_name, replace=True,
    )
    armed.append("trial_verdict_prompt")
    return armed


async def cancel_trial_jobs(
    session: AsyncSession, ctx: TenantContext, booking_id: uuid.UUID
) -> int:
    cancelled = 0
    for key in (
        jobs.dedupe("trial_reminder", booking_id, "24h"),
        jobs.dedupe("trial_reminder", booking_id, "2h"),
        jobs.dedupe("trial_verdict", booking_id),
        jobs.dedupe("trial_rebook", booking_id),
    ):
        cancelled += await jobs.cancel(session, ctx.id, dedupe_key=key)
    cancelled += await jobs.cancel(
        session, ctx.id, dedupe_prefix=f"trial_followup:{booking_id}"
    )
    return cancelled


async def schedule_trial_followup(
    session: AsyncSession, ctx: TenantContext, booking_id: uuid.UUID
) -> list[str]:
    """After an attended trial: +2 h, +1 d, +3 d, stopping on payment (ТЗ B5 У4)."""
    offsets = ctx.settings.trial.followup_hours or [2, 24, 72]
    armed: list[str] = []
    for step, hours in enumerate(offsets, start=1):
        await jobs.enqueue(
            session, ctx.id, "trial_followup", now_utc() + timedelta(hours=float(hours)),
            dedupe_key=jobs.dedupe("trial_followup", booking_id, step),
            payload={"booking_id": str(booking_id), "step": step},
            timezone=ctx.tz_name, quiet=ctx.settings.quiet, replace=True,
        )
        armed.append(f"trial_followup:{step}")
    return armed


async def schedule_trial_rebook(
    session: AsyncSession, ctx: TenantContext, booking_id: uuid.UUID
) -> None:
    await jobs.enqueue(
        session, ctx.id, "trial_rebook", now_utc() + timedelta(hours=1),
        dedupe_key=jobs.dedupe("trial_rebook", booking_id),
        payload={"booking_id": str(booking_id)},
        timezone=ctx.tz_name, quiet=ctx.settings.quiet,
    )


async def schedule_invoice_ladder(
    session: AsyncSession, ctx: TenantContext, invoice: Invoice
) -> list[str]:
    """Arm the whole debt ladder for one invoice; payment cancels the rest."""
    ladder = ctx.settings.billing.ladder_days
    armed: list[str] = []
    for step, run_at in bl.ladder_runs(
        invoice.due_date, ladder, timezone=ctx.tz_name
    ):
        if run_at <= now_utc() and step.offset_days < 0:
            continue  # a reminder before an already-passed due date is noise
        kind = "pay_handoff" if step.is_handoff else "pay_reminder"
        await jobs.enqueue(
            session, ctx.id, kind, run_at,
            dedupe_key=jobs.dedupe(kind, invoice.id, step.offset_days),
            payload={"invoice_id": str(invoice.id), "offset_days": step.offset_days},
            timezone=ctx.tz_name, quiet=ctx.settings.quiet, replace=True,
        )
        armed.append(f"{kind}:{step.offset_days}")
    return armed


async def cancel_invoice_ladder(
    session: AsyncSession, ctx: TenantContext, invoice_id: uuid.UUID
) -> int:
    cancelled = 0
    for kind in ("pay_reminder", "pay_handoff"):
        cancelled += await jobs.cancel(
            session, ctx.id, dedupe_prefix=f"{kind}:{invoice_id}"
        )
    return cancelled


async def schedule_enrollment_jobs(
    session: AsyncSession, ctx: TenantContext, enrollment_id: uuid.UUID
) -> list[str]:
    """Monthly invoice, renewal reminder and the 30-day review request."""
    from sqlalchemy import select

    enrollment = (
        await session.execute(
            select(Enrollment).where(
                Enrollment.tenant_id == ctx.id, Enrollment.id == enrollment_id
            )
        )
    ).scalar_one_or_none()
    if enrollment is None:
        return []

    armed: list[str] = []
    next_period_start = _next_month_first(enrollment.start_date)
    due = bl.due_date_for(
        bl.period_of(next_period_start), enrollment.billing_day,
        start_date=enrollment.start_date,
    )
    run_at = combine_local(
        due - timedelta(days=3), parse_hhmm("10:00"), ctx.tz_name
    )
    await jobs.enqueue(
        session, ctx.id, "invoice_create", max(run_at, now_utc() + timedelta(minutes=1)),
        dedupe_key=jobs.dedupe("invoice_create", enrollment_id, bl.period_of(next_period_start)),
        payload={
            "enrollment_id": str(enrollment_id),
            "period": bl.period_of(next_period_start),
        },
        timezone=ctx.tz_name, respect_quiet_hours=False,
    )
    armed.append("invoice_create")

    if enrollment.end_date:
        renew_at = combine_local(
            enrollment.end_date - timedelta(days=7), parse_hhmm("11:00"), ctx.tz_name
        )
        if renew_at > now_utc():
            await jobs.enqueue(
                session, ctx.id, "renew_reminder", renew_at,
                dedupe_key=jobs.dedupe("renew", enrollment_id),
                payload={"enrollment_id": str(enrollment_id)},
                timezone=ctx.tz_name, quiet=ctx.settings.quiet,
            )
            armed.append("renew_reminder")

    if ctx.settings.features.reviews:
        await jobs.enqueue(
            session, ctx.id, "study_review_request",
            combine_local(
                enrollment.start_date + timedelta(days=30), parse_hhmm("12:00"), ctx.tz_name
            ),
            dedupe_key=jobs.dedupe("study_review", enrollment_id),
            payload={"enrollment_id": str(enrollment_id)},
            timezone=ctx.tz_name, quiet=ctx.settings.quiet,
        )
        armed.append("study_review_request")
    return armed


def _next_month_first(day: date) -> date:
    return date(day.year + (day.month == 12), (day.month % 12) + 1, 1)


async def schedule_attendance_prompt(
    session: AsyncSession, ctx: TenantContext, lesson_id: uuid.UUID, ends_at
) -> None:
    """Ask the teacher 10 minutes before the lesson ends (ТЗ B6)."""
    await jobs.enqueue(
        session, ctx.id, "attendance_prompt", ends_at - timedelta(minutes=10),
        dedupe_key=jobs.dedupe("attendance_prompt", lesson_id),
        payload={"lesson_id": str(lesson_id)},
        timezone=ctx.tz_name, respect_quiet_hours=False,
    )


async def schedule_owner_report(session: AsyncSession, ctx: TenantContext) -> None:
    local_now = to_tenant(now_utc(), ctx.tz_name)
    target = parse_hhmm(ctx.settings.owner_report_at)
    day = local_now.date()
    run_at = combine_local(day, target, ctx.tz_name)
    if run_at <= now_utc():
        run_at = combine_local(day + timedelta(days=1), target, ctx.tz_name)
    await jobs.enqueue(
        session, ctx.id, "owner_report", run_at,
        dedupe_key=jobs.dedupe("owner_report", run_at.date().isoformat()),
        payload={}, timezone=ctx.tz_name, respect_quiet_hours=False,
    )


__all__ = [
    "cancel_invoice_ladder",
    "cancel_trial_jobs",
    "schedule_attendance_prompt",
    "schedule_enrollment_jobs",
    "schedule_invoice_ladder",
    "schedule_owner_report",
    "schedule_trial_followup",
    "schedule_trial_jobs",
    "schedule_trial_rebook",
]
