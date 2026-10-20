"""Education-centre job handlers (ТЗ B6)."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import Button
from arix.core.context import TenantContext
from arix.core.events import (
    ABSENCE,
    log_event,
)
from arix.core.timeutil import humanize_local, local_date, now_utc, to_tenant
from arix.db.models_core import Conversation, ScheduledJob
from arix.domain.messaging import send_message
from arix.domain.notify import notify_one, notify_staff
from arix.logging_setup import get_logger
from arix.templates import render

from . import attendance as att
from . import billing as bl
from . import enrollment as en
from . import trials as tr
from .models import Enrollment, Group, Invoice, Lesson, Student, Teacher
from .reports import build_daily_report, render_daily_report
from .scheduling import (
    cancel_invoice_ladder,
    schedule_invoice_ladder,
    schedule_owner_report,
)

log = get_logger("arix.edu.jobs")


async def _conversation_for_contact(
    session: AsyncSession, ctx: TenantContext, contact_id: uuid.UUID
) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.tenant_id == ctx.id, Conversation.contact_id == contact_id)
            .order_by(Conversation.last_inbound_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _tell_payers(
    session: AsyncSession,
    ctx: TenantContext,
    student_id: uuid.UUID,
    text: str,
    *,
    buttons: list[Button] | None = None,
    is_marketing: bool = False,
) -> int:
    """Send a message to whoever may hear about this learner (ТЗ B10)."""
    sent = 0
    for contact in await bl.payer_contacts(session, ctx.id, student_id):
        conversation = await _conversation_for_contact(session, ctx, contact.id)
        if conversation is None:
            continue
        outcome = await send_message(
            session, ctx, conversation, text,
            buttons=buttons, contact=contact, is_marketing=is_marketing,
        )
        sent += 1 if outcome.ok else 0
    return sent


# --- trials ---------------------------------------------------------------


async def trial_reminder(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    booking_id = uuid.UUID((job.payload or {})["booking_id"])
    refs = await tr.booking_with_refs(session, ctx.id, booking_id)
    if refs is None:
        return {"skipped": "no_booking"}
    booking, trial, student, course = refs
    if booking.status not in ("booked", "confirmed"):
        return {"skipped": f"status_{booking.status}"}

    offset = (job.payload or {}).get("offset", "24h")
    code = "trial_reminder_24h" if offset == "24h" else "trial_reminder_2h"
    text = render(
        code, ctx.default_lang, overrides=ctx.settings.texts,
        student=student.name,
        course=course.label(ctx.default_lang) if course else "занятие",
        when=humanize_local(trial.starts_at, ctx.tz_name, ctx.default_lang),
        address=ctx.settings.address or "",
        map_line=f"Как добраться: {ctx.settings.map_url}" if ctx.settings.map_url else "",
    )
    buttons = (
        [
            Button("Придём", f"trial:confirm:{booking.id}"),
            Button("Перенести", f"trial:move:{booking.id}"),
            Button("Отменить", f"trial:cancel:{booking.id}"),
        ]
        if offset == "24h"
        else None
    )
    sent = await _tell_payers(session, ctx, student.id, text, buttons=buttons)
    return {"sent": sent, "offset": offset}


async def trial_verdict_prompt(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """Ask the teacher for a one-tap verdict right after the trial."""
    booking_id = uuid.UUID((job.payload or {})["booking_id"])
    refs = await tr.booking_with_refs(session, ctx.id, booking_id)
    if refs is None:
        return {"skipped": "no_booking"}
    booking, trial, student, course = refs
    if booking.status not in ("booked", "confirmed"):
        return {"skipped": f"status_{booking.status}"}

    teacher_tg: int | None = None
    if trial.teacher_id:
        teacher = (
            await session.execute(select(Teacher).where(Teacher.id == trial.teacher_id))
        ).scalar_one_or_none()
        teacher_tg = teacher.tg_user_id if teacher else None

    text = (
        f"Пробное закончилось: {student.name}"
        + (f" · {course.label('ru')}" if course else "")
        + "\nКак прошло? Оценка в один тап:"
    )
    buttons = [
        ("👍 Готов идти", f"tb:score:{booking.id}:5"),
        ("🙂 Норм", f"tb:score:{booking.id}:4"),
        ("🤔 Сомнения", f"tb:score:{booking.id}:3"),
        ("❌ Не пришёл", f"tb:noshow:{booking.id}"),
    ]
    if teacher_tg:
        await notify_one(session, ctx, teacher_tg, text, buttons)
        return {"asked": "teacher"}
    sent = await notify_staff(session, ctx, roles=["admin"], text=text, buttons=buttons)
    return {"asked": "admin", "sent": sent}


async def trial_followup(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Nudge the parent after an attended trial (ТЗ B5 У4) — the main leak."""
    payload = job.payload or {}
    booking_id = uuid.UUID(payload["booking_id"])
    step = int(payload.get("step") or 1)
    refs = await tr.booking_with_refs(session, ctx.id, booking_id)
    if refs is None:
        return {"skipped": "no_booking"}
    booking, _trial, student, course = refs
    if booking.status != "attended":
        return {"skipped": f"status_{booking.status}"}
    if student.status == "active":
        return {"skipped": "already_enrolled"}

    group = await tr.recommend_group(session, ctx, booking)
    seats_line = ""
    price_line = ""
    schedule_line = ""
    if group is not None:
        offers = await en.list_groups(session, ctx, course_id=group.course_id, limit=4)
        offer = next((item for item in offers if item.group_id == group.id), None)
        if offer is not None:
            seats_line = f"осталось мест: {offer.seats_left}"
            price_line = en.price_text(offer)
            schedule_line = offer.schedule_text

    text = render(
        f"trial_followup_{min(step, 3)}", ctx.default_lang, overrides=ctx.settings.texts,
        student=student.name,
        course=course.label(ctx.default_lang) if course else "курс",
        teacher_note=booking.teacher_note or "",
        group=group.name if group else "",
        schedule=schedule_line,
        price=price_line,
        seats=seats_line,
    )
    buttons = (
        [Button("Записать в группу", f"edu:enroll:{group.id}:{student.id}")]
        if group is not None
        else None
    )
    sent = await _tell_payers(session, ctx, student.id, text, buttons=buttons)
    return {"sent": sent, "step": step}


async def trial_rebook(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    booking_id = uuid.UUID((job.payload or {})["booking_id"])
    refs = await tr.booking_with_refs(session, ctx.id, booking_id)
    if refs is None:
        return {"skipped": "no_booking"}
    booking, trial, student, course = refs
    if booking.status != "no_show":
        return {"skipped": f"status_{booking.status}"}

    offers = await tr.find_trials(
        session, ctx, course_id=trial.course_id, group_id=trial.group_id, limit=2
    )
    text = render(
        "trial_rebook", ctx.default_lang, overrides=ctx.settings.texts,
        student=student.name,
        course=course.label(ctx.default_lang) if course else "занятие",
        slots=", ".join(
            humanize_local(offer.starts_at, ctx.tz_name, ctx.default_lang) for offer in offers
        )
        or "другое время",
    )
    buttons = [
        Button(to_tenant(offer.starts_at, ctx.tz_name).strftime("%d.%m %H:%M"),
               f"trial:book:{offer.trial_id}:{student.id}")
        for offer in offers
    ]
    sent = await _tell_payers(session, ctx, student.id, text, buttons=buttons or None)
    return {"sent": sent}


# --- money ----------------------------------------------------------------


async def invoice_create(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Issue next month's invoice and arm its ladder."""
    payload = job.payload or {}
    enrollment_id = uuid.UUID(payload["enrollment_id"])
    period = payload.get("period") or bl.period_of(local_date(now_utc(), ctx.tz_name))
    enrollment = (
        await session.execute(
            select(Enrollment).where(
                Enrollment.tenant_id == ctx.id, Enrollment.id == enrollment_id
            )
        )
    ).scalar_one_or_none()
    if enrollment is None or enrollment.status not in ("active", "reserved"):
        return {"skipped": "inactive_enrollment"}

    refs = await en.group_with_refs(session, ctx.id, enrollment.group_id)
    invoice = await bl.create_invoice(
        session, ctx, enrollment, period, group=refs[0] if refs else None
    )
    if invoice is None:
        return {"skipped": "already_issued_or_zero"}
    await schedule_invoice_ladder(session, ctx, invoice)

    # Arm the following month so billing keeps rolling without a cron per tenant.
    from .scheduling import schedule_enrollment_jobs

    await schedule_enrollment_jobs(session, ctx, enrollment_id)
    return {"invoice_id": str(invoice.id), "amount": float(invoice.amount), "period": period}


async def pay_reminder(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    payload = job.payload or {}
    invoice_id = uuid.UUID(payload["invoice_id"])
    offset = int(payload.get("offset_days") or 0)
    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.tenant_id == ctx.id, Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        return {"skipped": "no_invoice"}
    if invoice.status == "paid":
        await cancel_invoice_ladder(session, ctx, invoice_id)
        return {"skipped": "already_paid"}

    today = local_date(now_utc(), ctx.tz_name)
    if invoice.status == "open" and invoice.due_date < today:
        invoice.status = "overdue"
        await session.flush()

    code = (
        "pay_reminder_before" if offset < 0
        else "pay_reminder_due" if offset == 0
        else "pay_reminder_overdue"
    )
    student = (
        await session.execute(select(Student).where(Student.id == invoice.student_id))
    ).scalar_one()
    link = None
    if ctx.settings.features.miniapp_payments:
        from arix.payments import create_payment

        payers = await bl.payer_contacts(session, ctx.id, student.id)
        if payers:
            _payment, link = await create_payment(
                session, ctx,
                contact_id=payers[0].id, amount=float(invoice.amount),
                entity_ref=str(invoice.id), attributed_to="miniapp",
                currency=invoice.currency,
            )

    text = render(
        code, ctx.default_lang, overrides=ctx.settings.texts,
        student=student.name,
        amount=_money(float(invoice.amount)),
        currency={"KZT": "₸", "UZS": "сум", "RUB": "₽"}.get(invoice.currency, invoice.currency),
        period=invoice.period,
        due=invoice.due_date.strftime("%d.%m"),
        days=bl.debt_age_days(invoice.due_date, today),
        pay_line=f"Оплатить: {link}" if link else "",
    )
    sent = await _tell_payers(session, ctx, student.id, text)
    return {"sent": sent, "offset": offset, "status": invoice.status}


async def pay_handoff(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Last ladder step: a human takes over the debt (ТЗ B5 У6)."""
    invoice_id = uuid.UUID((job.payload or {})["invoice_id"])
    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.tenant_id == ctx.id, Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        return {"skipped": "no_invoice"}
    if invoice.status == "paid":
        return {"skipped": "already_paid"}
    student = (
        await session.execute(select(Student).where(Student.id == invoice.student_id))
    ).scalar_one()
    today = local_date(now_utc(), ctx.tz_name)
    contacts = await bl.payer_contacts(session, ctx.id, student.id)
    phones = ", ".join(filter(None, (contact.phone for contact in contacts))) or "телефон не указан"
    sent = await notify_staff(
        session, ctx, roles=["admin", "owner"],
        text=(
            f"💰 Долг {bl.debt_age_days(invoice.due_date, today)} дн.\n"
            f"{student.name} · {_money(float(invoice.amount))} {invoice.currency}\n"
            f"Период {invoice.period}, срок был {invoice.due_date:%d.%m}\n"
            f"Контакт: {phones}"
        ),
    )
    return {"notified": sent}


async def renew_reminder(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    enrollment_id = uuid.UUID((job.payload or {})["enrollment_id"])
    enrollment = (
        await session.execute(
            select(Enrollment).where(
                Enrollment.tenant_id == ctx.id, Enrollment.id == enrollment_id
            )
        )
    ).scalar_one_or_none()
    if enrollment is None or enrollment.status != "active":
        return {"skipped": "inactive"}
    refs = await en.group_with_refs(session, ctx.id, enrollment.group_id)
    student = (
        await session.execute(select(Student).where(Student.id == enrollment.student_id))
    ).scalar_one()
    text = render(
        "renew_reminder", ctx.default_lang, overrides=ctx.settings.texts,
        student=student.name,
        group=refs[0].name if refs else "",
        course=refs[1].label(ctx.default_lang) if refs else "",
        until=enrollment.end_date.strftime("%d.%m") if enrollment.end_date else "",
    )
    sent = await _tell_payers(session, ctx, student.id, text)
    return {"sent": sent}


async def next_level_offer(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    enrollment_id = uuid.UUID((job.payload or {})["enrollment_id"])
    enrollment = (
        await session.execute(
            select(Enrollment).where(
                Enrollment.tenant_id == ctx.id, Enrollment.id == enrollment_id
            )
        )
    ).scalar_one_or_none()
    if enrollment is None:
        return {"skipped": "no_enrollment"}
    refs = await en.group_with_refs(session, ctx.id, enrollment.group_id)
    if refs is None:
        return {"skipped": "no_group"}
    group, course, _teacher = refs
    offers = [
        offer
        for offer in await en.list_groups(session, ctx, course_id=course.id, limit=6)
        if offer.group_id != group.id
    ]
    if not offers:
        return {"skipped": "no_next_group"}
    student = (
        await session.execute(select(Student).where(Student.id == enrollment.student_id))
    ).scalar_one()
    offer = offers[0]
    text = render(
        "next_level_offer", ctx.default_lang, overrides=ctx.settings.texts,
        student=student.name,
        level=offer.level or "следующий уровень",
        group=offer.name,
        schedule=offer.schedule_text,
        price=en.price_text(offer),
        starts=offer.start_date.strftime("%d.%m") if offer.start_date else "уточним",
    )
    sent = await _tell_payers(
        session, ctx, student.id, text,
        buttons=[Button("Записать", f"edu:enroll:{offer.group_id}:{student.id}")],
    )
    return {"sent": sent}


# --- lessons and attendance ------------------------------------------------


async def attendance_prompt(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """10 minutes before the lesson ends, send the roster to the teacher."""
    if not ctx.settings.features.attendance:
        return {"skipped": "disabled"}
    lesson_id = uuid.UUID((job.payload or {})["lesson_id"])
    pair = await att.lesson_with_group(session, ctx.id, lesson_id)
    if pair is None:
        return {"skipped": "no_lesson"}
    lesson, group = pair
    if lesson.status != "planned":
        return {"skipped": f"status_{lesson.status}"}

    students = await en.group_students(session, ctx.id, group.id)
    if not students:
        return {"skipped": "empty_group"}

    teacher_tg: int | None = None
    if group.teacher_id:
        teacher = (
            await session.execute(select(Teacher).where(Teacher.id == group.teacher_id))
        ).scalar_one_or_none()
        teacher_tg = teacher.tg_user_id if teacher else None

    text = (
        f"Посещаемость: {group.name}, "
        f"{to_tenant(lesson.starts_at, ctx.tz_name):%d.%m %H:%M}\n"
        "Отметьте, кого НЕ было. Остальные — «был».\n"
        + "\n".join(f"• {student.name}" for student in students)
    )
    buttons = [
        (f"нет: {student.name.split()[0]}", f"at:absent:{lesson.id}:{student.id}")
        for student in students[:8]
    ]
    buttons.append(("✅ Все были", f"at:done:{lesson.id}"))

    if teacher_tg:
        await notify_one(session, ctx, teacher_tg, text, buttons)
        return {"asked": "teacher", "students": len(students)}
    sent = await notify_staff(session, ctx, roles=["admin"], text=text, buttons=buttons)
    return {"asked": "admin", "sent": sent}


async def absence_notice(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Tell the guardian the same day, and flag churn risk (ТЗ B5 У7)."""
    lesson_id = uuid.UUID((job.payload or {})["lesson_id"])
    pending = await att.pending_notices(session, ctx, lesson_id)
    if not pending:
        return {"sent": 0}
    pair = await att.lesson_with_group(session, ctx.id, lesson_id)
    group_name = pair[1].name if pair else ""
    sent = 0
    risks: list[str] = []
    for student, mark in pending:
        text = render(
            "absence_notice", ctx.default_lang, overrides=ctx.settings.texts,
            student=student.name,
            group=group_name,
            when=to_tenant(pair[0].starts_at, ctx.tz_name).strftime("%d.%m %H:%M")
            if pair
            else "",
        )
        sent += await _tell_payers(session, ctx, student.id, text)
        mark.notified_at = now_utc()
        await log_event(
            session, ctx.id, ABSENCE, contact_id=student.contact_id,
            entity_ref=str(lesson_id), data={"student": student.name, "notified": True},
        )
        risk = await att.flag_churn_risk(session, ctx, student.id)
        if risk:
            risks.append(
                f"{risk['name']}: {risk['consecutive_absences']} подряд, "
                f"{risk['absences_30d']} за месяц"
            )
    await session.flush()
    if risks:
        await notify_staff(
            session, ctx, roles=["admin", "owner"],
            text="⚠️ Риск ухода:\n" + "\n".join(risks),
        )
    return {"sent": sent, "risks": len(risks)}


async def lesson_cancelled_notice(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """Tell the whole group within a minute (ТЗ B5 У11)."""
    payload = job.payload or {}
    lesson_id = uuid.UUID(payload["lesson_id"])
    pair = await att.lesson_with_group(session, ctx.id, lesson_id)
    if pair is None:
        return {"skipped": "no_lesson"}
    lesson, group = pair
    students = await en.group_students(session, ctx.id, group.id)
    moved_to = payload.get("moved_to")
    text = render(
        "lesson_moved" if moved_to else "lesson_cancelled",
        ctx.default_lang, overrides=ctx.settings.texts,
        group=group.name,
        when=to_tenant(lesson.starts_at, ctx.tz_name).strftime("%d.%m %H:%M"),
        new_when=moved_to or "",
        reason=lesson.cancel_reason or "",
    )
    sent = 0
    for student in students:
        sent += await _tell_payers(session, ctx, student.id, text)
    return {"sent": sent, "students": len(students)}


# --- lifecycle ------------------------------------------------------------


async def group_start_announce(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """A recruiting group reached its minimum: tell everyone it starts."""
    ready = await en.recruiting_groups_ready(session, ctx)
    announced = 0
    for group, taken in ready:
        refs = await en.group_with_refs(session, ctx.id, group.id)
        course = refs[1] if refs else None
        students = await en.group_students(session, ctx.id, group.id)
        text = render(
            "group_starts", ctx.default_lang, overrides=ctx.settings.texts,
            group=group.name,
            course=course.label(ctx.default_lang) if course else "",
            schedule=en.schedule_text(group.schedule),
            starts=group.start_date.strftime("%d.%m") if group.start_date else "уточним",
            taken=taken,
        )
        for student in students:
            await _tell_payers(session, ctx, student.id, text)
        group.status = "running"
        await en.generate_lessons(session, ctx, group, weeks=4)
        announced += 1
    await session.flush()
    return {"announced": announced}


async def study_review_request(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """30 days in: ask for a score (ТЗ B5 У12)."""
    enrollment_id = uuid.UUID((job.payload or {})["enrollment_id"])
    enrollment = (
        await session.execute(
            select(Enrollment).where(
                Enrollment.tenant_id == ctx.id, Enrollment.id == enrollment_id
            )
        )
    ).scalar_one_or_none()
    if enrollment is None or enrollment.status != "active":
        return {"skipped": "inactive"}
    student = (
        await session.execute(select(Student).where(Student.id == enrollment.student_id))
    ).scalar_one()
    text = render("study_review_request", ctx.default_lang, overrides=ctx.settings.texts,
                  student=student.name)
    buttons = [
        Button("★" * score, f"review:{enrollment.id}:{score}") for score in range(1, 6)
    ]
    sent = await _tell_payers(session, ctx, student.id, text, buttons=buttons)
    return {"sent": sent}


async def parent_report(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Monthly study digest for guardians (ТЗ B5 У8, v2)."""
    if not ctx.settings.features.parent_reports:
        return {"skipped": "disabled"}
    sent = 0
    enrollments = list(
        (
            await session.execute(
                select(Enrollment).where(
                    Enrollment.tenant_id == ctx.id, Enrollment.status == "active"
                )
            )
        ).scalars()
    )
    for enrollment in enrollments:
        stats = await att.student_attendance_report(session, ctx, enrollment.student_id)
        if not stats["total"]:
            continue
        student = (
            await session.execute(select(Student).where(Student.id == enrollment.student_id))
        ).scalar_one()
        refs = await en.group_with_refs(session, ctx.id, enrollment.group_id)
        text = render(
            "parent_report", ctx.default_lang, overrides=ctx.settings.texts,
            student=student.name,
            group=refs[0].name if refs else "",
            present=stats["present"],
            total=stats["total"],
            rate=int((stats["rate"] or 0) * 100),
            level=student.level or "—",
        )
        sent += await _tell_payers(session, ctx, student.id, text)
    return {"sent": sent}


async def reactivation(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Seasonal offer to students who left (ТЗ B5 У13) — marketing consent only."""
    students = list(
        (
            await session.execute(
                select(Student).where(Student.tenant_id == ctx.id, Student.status == "left")
            )
        ).scalars()
    )
    sent = 0
    for student in students:
        text = render(
            "reactivation", ctx.default_lang, overrides=ctx.settings.texts,
            student=student.name,
        )
        sent += await _tell_payers(
            session, ctx, student.id, text, is_marketing=True,
        )
    return {"sent": sent, "candidates": len(students)}


async def owner_report(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    report = await build_daily_report(session, ctx)
    text = render_daily_report(ctx, report)
    sent = await notify_staff(
        session, ctx, roles=["owner", "admin"], text=text,
        buttons=[("Долги", "rep:debts"), ("Группы", "rep:groups"), ("Пробные", "rep:trials")],
    )
    await schedule_owner_report(session, ctx)
    await bl.refresh_overdue(session, ctx)
    return {"sent": sent, "day": report.day.isoformat()}


async def lessons_rollout(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """Keep four weeks of lessons materialised and their prompts armed."""
    from .scheduling import schedule_attendance_prompt

    groups = list(
        (
            await session.execute(
                select(Group).where(Group.tenant_id == ctx.id, Group.status == "running")
            )
        ).scalars()
    )
    created = armed = 0
    for group in groups:
        created += await en.generate_lessons(session, ctx, group, weeks=4)

    upcoming = list(
        (
            await session.execute(
                select(Lesson).where(
                    Lesson.tenant_id == ctx.id,
                    Lesson.status == "planned",
                    Lesson.starts_at > now_utc(),
                    Lesson.starts_at < now_utc() + timedelta(days=2),
                )
            )
        ).scalars()
    )
    for lesson in upcoming:
        await schedule_attendance_prompt(
            session, ctx, lesson.id,
            lesson.starts_at + timedelta(minutes=lesson.duration_min),
        )
        armed += 1
    return {"lessons_created": created, "prompts_armed": armed}


# --- providers the core handlers ask for -----------------------------------


async def slot_hint(session: AsyncSession, ctx: TenantContext, payload: dict) -> str | None:
    """Two concrete trial dates for the generic follow-up text."""
    courses = await en.list_courses(
        session, ctx.id, query=payload.get("service"), limit=1
    )
    course_id = courses[0].id if courses else None
    offers = await tr.find_trials(session, ctx, course_id=course_id, limit=2)
    if not offers:
        return None
    return ", ".join(
        humanize_local(offer.starts_at, ctx.tz_name, ctx.default_lang) for offer in offers
    )


def _money(value: float) -> str:
    return f"{int(value):,}".replace(",", " ")


EDU_HANDLERS = {
    "trial_reminder": trial_reminder,
    "trial_verdict_prompt": trial_verdict_prompt,
    "trial_followup": trial_followup,
    "trial_rebook": trial_rebook,
    "invoice_create": invoice_create,
    "pay_reminder": pay_reminder,
    "pay_handoff": pay_handoff,
    "renew_reminder": renew_reminder,
    "next_level_offer": next_level_offer,
    "attendance_prompt": attendance_prompt,
    "absence_notice": absence_notice,
    "lesson_cancelled_notice": lesson_cancelled_notice,
    "group_start_announce": group_start_announce,
    "study_review_request": study_review_request,
    "parent_report": parent_report,
    "reactivation": reactivation,
    "owner_report": owner_report,
    "lessons_rollout": lessons_rollout,
}


async def arm_daily(session, ctx) -> None:
    """Education-centre jobs armed every day from the shared worker."""
    from arix.core.timeutil import local_date, now_utc
    from arix.scheduler.jobs import arm_local

    today = local_date(now_utc(), ctx.tz_name)
    await arm_local(session, ctx, "lessons_rollout", "03:00")
    await arm_local(session, ctx, "group_start_announce", "10:00")
    if today.month in (8, 12) and today.day == 1:
        await arm_local(session, ctx, "reactivation", "11:00", roll_forward=False)
    if today.day == 1 and ctx.settings.features.parent_reports:
        await arm_local(session, ctx, "parent_report", "12:00", roll_forward=False)


def register() -> None:
    """Wire the edu module into the shared scheduler."""
    from arix.scheduler.registry import (
        register_daily,
        register_handlers,
        register_report_builder,
        register_slot_hint,
    )

    register_handlers(EDU_HANDLERS)
    register_daily(arm_daily)
    register_slot_hint(slot_hint)
    register_report_builder(lambda session, ctx, day: build_daily_report(session, ctx, day))


__all__ = ["EDU_HANDLERS", "register"]
