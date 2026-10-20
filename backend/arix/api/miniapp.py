"""Mini App for a parent, an adult learner or a teacher.

Authentication is the Telegram ``initData`` signature. Every endpoint derives
the contact from that signature, and a parent only ever sees their own
students (``students_of_contact``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import date
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from arix.core.context import TenantContext, load_context_by_slug
from arix.core.crypto import decrypt_json
from arix.core.timeutil import humanize_local, now_utc
from arix.db.models_core import Channel, Contact, ContactIdentity, Conversation, Staff
from arix.db.session import tenant_session
from arix.verticals.edu import attendance as att
from arix.verticals.edu import billing as bl
from arix.verticals.edu import enrollment as en
from arix.verticals.edu import trials as tr
from arix.verticals.edu.models import Course, Enrollment, Group, Lesson

router = APIRouter(prefix="/miniapp/{slug}", tags=["miniapp"])

INIT_DATA_TTL_SECONDS = 24 * 3600


class TrialBookIn(BaseModel):
    trial_id: uuid.UUID
    student_name: str
    birth_year: int | None = None


class PayInvoiceIn(BaseModel):
    invoice_id: uuid.UUID


def verify_init_data(init_data: str, bot_token: str) -> dict:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    provided = pairs.pop("hash", None)
    if not provided:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData without hash")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad initData signature")
    auth_date = int(pairs.get("auth_date") or 0)
    if auth_date and (now_utc().timestamp() - auth_date) > INIT_DATA_TTL_SECONDS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData expired")
    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData without user")
    return user


class MiniAppScope:
    def __init__(self, session, ctx: TenantContext, contact: Contact, conversation: Conversation):
        self.session = session
        self.ctx = ctx
        self.contact = contact
        self.conversation = conversation


async def miniapp_scope(
    slug: str,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
):
    if not x_telegram_init_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Telegram-Init-Data required")
    async with tenant_session(None, bypass_rls=True) as probe:
        ctx = await load_context_by_slug(probe, slug)
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown tenant")

    async with tenant_session(ctx.id) as session:
        channel = (
            await session.execute(
                select(Channel).where(
                    Channel.tenant_id == ctx.id, Channel.type.in_(["tg", "tg_business"])
                )
            )
        ).scalars().first()
        creds = decrypt_json(channel.credentials_enc) if channel else {}
        bot_token = creds.get("bot_token")
        if not bot_token:
            from arix.config import get_settings

            bot_token = get_settings().telegram_shared_bot_token
        if not bot_token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telegram is not connected")

        user = verify_init_data(x_telegram_init_data, bot_token)
        peer_id = str(user["id"])

        identity = (
            await session.execute(
                select(ContactIdentity).where(
                    ContactIdentity.tenant_id == ctx.id,
                    ContactIdentity.channel_type == "tg",
                    ContactIdentity.external_id == peer_id,
                )
            )
        ).scalar_one_or_none()
        contact = None
        if identity is not None:
            contact = (
                await session.execute(select(Contact).where(Contact.id == identity.contact_id))
            ).scalar_one_or_none()
        if contact is None:
            contact = Contact(
                tenant_id=ctx.id,
                name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None,
                lang=ctx.lang_for(user.get("language_code")),
                source="miniapp",
            )
            session.add(contact)
            await session.flush()
            session.add(
                ContactIdentity(
                    tenant_id=ctx.id, contact_id=contact.id,
                    channel_type="tg", external_id=peer_id,
                )
            )
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == ctx.id,
                    Conversation.channel_type == "tg",
                    Conversation.peer_id == peer_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(
                tenant_id=ctx.id, contact_id=contact.id, channel_id=channel.id if channel else None,
                channel_type="tg", peer_id=peer_id, state="active",
            )
            session.add(conversation)
        await session.flush()
        yield MiniAppScope(session, ctx, contact, conversation)


Scope = Annotated[MiniAppScope, Depends(miniapp_scope)]


@router.get("/profile")
async def profile(scope: Scope) -> dict:
    students = await en.students_of_contact(scope.session, scope.ctx.id, scope.contact.id)
    return {
        "tenant": {
            "name": scope.ctx.name,
            "city": scope.ctx.tenant.city,
            "address": scope.ctx.settings.address,
            "phone": scope.ctx.settings.phone,
            "currency": scope.ctx.currency,
            "languages": scope.ctx.languages,
            "payments_enabled": scope.ctx.settings.features.miniapp_payments,
        },
        "contact": {
            "name": scope.contact.name,
            "phone": scope.contact.phone,
            "consent_pd": scope.contact.consent_pd_at is not None,
            "lang": scope.contact.lang,
        },
        "students": [{"id": str(row.id), "name": row.name, "status": row.status} for row in students],
    }


@router.get("/courses")
async def courses(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Course).where(Course.tenant_id == scope.ctx.id, Course.active.is_(True))
        )
    ).scalars()
    lang = scope.contact.lang
    return [
        {
            "course_id": str(row.id),
            "name": row.label(lang),
            "price_month": float(row.price_month) if row.price_month is not None else None,
            "currency": row.currency,
            "has_trial": row.has_trial,
        }
        for row in rows
    ]


@router.get("/groups")
async def groups(scope: Scope, course_id: uuid.UUID | None = None) -> list[dict]:
    offers = await en.list_groups(scope.session, scope.ctx, course_id=course_id)
    return [
        {
            "group_id": str(offer.group_id),
            "name": offer.name,
            "schedule": offer.schedule_text,
            "seats_left": offer.seats_left,
            "price_month": offer.price_month,
            "currency": offer.currency,
        }
        for offer in offers
    ]


@router.get("/schedule")
async def schedule(scope: Scope) -> list[dict]:
    students = await en.students_of_contact(scope.session, scope.ctx.id, scope.contact.id)
    if not students:
        return []
    student_ids = [row.id for row in students]
    rows = (
        await scope.session.execute(
            select(Lesson, Group)
            .join(Group, Group.id == Lesson.group_id)
            .join(Enrollment, Enrollment.group_id == Group.id)
            .where(
                Lesson.tenant_id == scope.ctx.id,
                Enrollment.student_id.in_(student_ids),
                Enrollment.status.in_(["reserved", "active"]),
                Lesson.status == "planned",
                Lesson.starts_at >= now_utc(),
            )
            .order_by(Lesson.starts_at)
            .limit(20)
        )
    ).all()
    lang = scope.contact.lang
    return [
        {
            "lesson_id": str(lesson.id),
            "group": group.name,
            "when": humanize_local(lesson.starts_at, scope.ctx.tz_name, lang),
            "starts_at": lesson.starts_at,
        }
        for lesson, group in rows
    ]


@router.get("/balance")
async def balance(scope: Scope) -> dict:
    students = await en.students_of_contact(scope.session, scope.ctx.id, scope.contact.id)
    items = []
    for student in students:
        snapshot = await bl.student_balance(scope.session, scope.ctx, student.id)
        items.append({"student_id": str(student.id), "name": student.name, **snapshot})
    return {"students": items}


@router.get("/trials")
async def trials(scope: Scope, course_id: uuid.UUID | None = None) -> list[dict]:
    offers = await tr.find_trials(scope.session, scope.ctx, course_id=course_id, limit=8)
    lang = scope.contact.lang
    return [
        {
            "trial_id": str(offer.trial_id),
            "when": humanize_local(offer.starts_at, scope.ctx.tz_name, lang),
            "seats_left": offer.seats_left,
            "course": offer.course_name,
        }
        for offer in offers
    ]


@router.post("/trials")
async def book_trial(body: TrialBookIn, scope: Scope) -> dict:
    student = await en.ensure_student(
        scope.session, scope.ctx,
        contact=scope.contact,
        name=body.student_name,
        birth_year=body.birth_year,
    )
    try:
        booking = await tr.book_trial(
            scope.session, scope.ctx,
            trial_id=body.trial_id,
            student_id=student.id,
            attribution="miniapp",
            idempotency_key=f"mini:{scope.contact.id}:{body.trial_id}:{student.id}",
        )
    except tr.TrialFull as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "trial is full") from exc
    from arix.verticals.edu.scheduling import schedule_trial_jobs

    await schedule_trial_jobs(scope.session, scope.ctx, booking.id)
    return {"booking_id": str(booking.id), "student_id": str(student.id)}


@router.post("/pay")
async def pay(body: PayInvoiceIn, scope: Scope) -> dict:
    from arix.payments import create_payment
    from arix.verticals.edu.models import Invoice

    if not scope.ctx.settings.features.miniapp_payments:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "payments are disabled for this tenant")
    students = await en.students_of_contact(scope.session, scope.ctx.id, scope.contact.id)
    allowed = {row.id for row in students}
    invoice = (
        await scope.session.execute(
            select(Invoice).where(
                Invoice.tenant_id == scope.ctx.id, Invoice.id == body.invoice_id
            )
        )
    ).scalar_one_or_none()
    if invoice is None or invoice.student_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown invoice")
    payment, url = await create_payment(
        scope.session, scope.ctx,
        amount=invoice.amount,
        currency=invoice.currency,
        contact_id=scope.contact.id,
        entity_ref=str(invoice.id),
        attributed_to="miniapp",
    )
    return {"payment_id": str(payment.id), "url": url}


@router.get("/teacher/today")
async def teacher_today(scope: Scope, day: date | None = None) -> dict:
    """Today's groups for a teacher logged in with the same Telegram account."""
    member = (
        await scope.session.execute(
            select(Staff).where(
                Staff.tenant_id == scope.ctx.id,
                Staff.tg_user_id == int(scope.conversation.peer_id),
                Staff.role == "teacher",
                Staff.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if member is None or member.entity_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "teacher only")
    rows = (
        await scope.session.execute(
            select(Lesson, Group)
            .join(Group, Group.id == Lesson.group_id)
            .where(
                Lesson.tenant_id == scope.ctx.id,
                Group.teacher_id == member.entity_id,
                Lesson.status == "planned",
            )
            .order_by(Lesson.starts_at)
            .limit(12)
        )
    ).all()
    return {
        "lessons": [
            {
                "lesson_id": str(lesson.id),
                "group": group.name,
                "starts_at": lesson.starts_at,
            }
            for lesson, group in rows
        ]
    }


@router.get("/attendance/{student_id}")
async def student_attendance(student_id: uuid.UUID, scope: Scope) -> dict:
    students = await en.students_of_contact(scope.session, scope.ctx.id, scope.contact.id)
    if student_id not in {row.id for row in students}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown student")
    return await att.student_attendance_report(scope.session, scope.ctx, student_id)


__all__ = ["router", "verify_init_data"]
