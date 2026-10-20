"""Tenant panel for an education centre.

Courses, groups, students, invoices and attendance. Dialogues, the knowledge
base and the owner report stay on the shared models.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from arix.core.timeutil import now_utc
from arix.db.models_core import (
    Contact,
    Conversation,
    DailyMetric,
    Event,
    Handoff,
    KBItem,
    Message,
    Payment,
)
from arix.payments import mark_paid
from arix.verticals.edu import attendance as att
from arix.verticals.edu import billing as bl
from arix.verticals.edu import enrollment as en
from arix.verticals.edu.models import (
    Course,
    Enrollment,
    Group,
    Invoice,
    Lesson,
    Student,
    Teacher,
    TrialLesson,
)
from arix.verticals.edu.reports import build_daily_report, render_daily_report
from arix.verticals.edu.scheduling import schedule_enrollment_jobs
from arix.verticals.edu.settlement import settle_payment

from .deps import TenantSession, tenant_scope
from .schemas import KBItemIn

router = APIRouter(prefix="/panel/{tenant_id}", tags=["panel"])

Scope = Annotated[TenantSession, Depends(tenant_scope)]


class CourseIn(BaseModel):
    code: str
    name: dict[str, str]
    direction: str = "lang"
    price_month: float | None = None
    currency: str = "UZS"
    lessons_per_week: int = 2
    lesson_min: int = 90
    age_from: int | None = None
    age_to: int | None = None
    level_from: str | None = None
    level_to: str | None = None
    has_trial: bool = True
    has_level_test: bool = False
    goal_keywords: list[str] = Field(default_factory=list)


class GroupIn(BaseModel):
    course_id: uuid.UUID
    name: str
    teacher_id: uuid.UUID | None = None
    level: str | None = None
    schedule: dict = Field(default_factory=dict)
    capacity: int = 10
    min_to_start: int = 4
    start_date: date | None = None
    price_month: float | None = None
    room: str | None = None


class StudentIn(BaseModel):
    name: str
    birth_year: int | None = None
    phone: str | None = None
    contact_name: str | None = None


class EnrollIn(BaseModel):
    student_id: uuid.UUID
    group_id: uuid.UUID
    discount_pct: int = 0
    start_date: date | None = None


class AttendanceIn(BaseModel):
    lesson_id: uuid.UUID
    absent_student_ids: list[uuid.UUID] = Field(default_factory=list)


class LessonCancelIn(BaseModel):
    reason: str | None = None
    moved_to: datetime | None = None


# ---------------------------------------------------------------- courses ---
@router.get("/courses")
async def list_courses(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Course)
            .where(Course.tenant_id == scope.ctx.id, Course.active.is_(True))
            .order_by(Course.sort, Course.code)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "code": row.code,
            "name": row.label("ru"),
            "direction": row.direction,
            "price_month": float(row.price_month) if row.price_month is not None else None,
            "currency": row.currency,
            "lessons_per_week": row.lessons_per_week,
            "has_trial": row.has_trial,
            "has_level_test": row.has_level_test,
        }
        for row in rows
    ]


@router.post("/courses", status_code=201)
async def create_course(body: CourseIn, scope: Scope) -> dict:
    row = Course(
        tenant_id=scope.ctx.id,
        code=body.code,
        name=body.name,
        direction=body.direction,
        price_month=Decimal(str(body.price_month)) if body.price_month is not None else None,
        currency=body.currency,
        lessons_per_week=body.lessons_per_week,
        lesson_min=body.lesson_min,
        age_from=body.age_from,
        age_to=body.age_to,
        level_from=body.level_from,
        level_to=body.level_to,
        has_trial=body.has_trial,
        has_level_test=body.has_level_test,
        goal_keywords=body.goal_keywords,
    )
    scope.session.add(row)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.get("/groups")
async def list_groups(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Group).where(Group.tenant_id == scope.ctx.id).order_by(Group.name)
        )
    ).scalars()
    out = []
    for group in rows:
        taken = await en.seats_taken(scope.session, scope.ctx.id, group.id)
        out.append(
            {
                "id": str(group.id),
                "name": group.name,
                "course_id": str(group.course_id),
                "course": group.course.label("ru") if group.course else None,
                "status": group.status,
                "capacity": group.capacity,
                "taken": taken,
                "min_to_start": group.min_to_start,
                "short": max(0, group.min_to_start - taken),
                "schedule": group.schedule,
                "level": group.level,
                "start_date": group.start_date,
            }
        )
    return out


@router.post("/groups", status_code=201)
async def create_group(body: GroupIn, scope: Scope) -> dict:
    row = Group(
        tenant_id=scope.ctx.id,
        course_id=body.course_id,
        teacher_id=body.teacher_id,
        name=body.name,
        level=body.level,
        schedule=body.schedule,
        capacity=body.capacity,
        min_to_start=body.min_to_start,
        start_date=body.start_date,
        price_month=Decimal(str(body.price_month)) if body.price_month is not None else None,
        room=body.room,
    )
    scope.session.add(row)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.get("/teachers")
async def list_teachers(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Teacher).where(Teacher.tenant_id == scope.ctx.id, Teacher.active.is_(True))
        )
    ).scalars()
    return [{"id": str(row.id), "name": row.name, "subjects": list(row.subjects or [])} for row in rows]


@router.get("/students")
async def list_students(scope: Scope, status_filter: str | None = None) -> list[dict]:
    stmt = select(Student).where(Student.tenant_id == scope.ctx.id).order_by(Student.name)
    if status_filter:
        stmt = stmt.where(Student.status == status_filter)
    rows = (await scope.session.execute(stmt.limit(500))).scalars()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "level": row.level,
            "birth_year": row.birth_year,
            "churn_risk_at": row.churn_risk_at,
        }
        for row in rows
    ]


@router.post("/students", status_code=201)
async def create_student(body: StudentIn, scope: Scope) -> dict:
    contact = None
    if body.phone:
        contact = Contact(
            tenant_id=scope.ctx.id,
            name=body.contact_name or body.name,
            phone=body.phone,
            lang=scope.ctx.default_lang,
            source="admin",
        )
        scope.session.add(contact)
        await scope.session.flush()
    student = Student(
        tenant_id=scope.ctx.id,
        contact_id=contact.id if contact else None,
        name=body.name,
        birth_year=body.birth_year,
        status="lead",
    )
    scope.session.add(student)
    await scope.session.flush()
    return {"id": str(student.id), "contact_id": str(contact.id) if contact else None}


@router.get("/enrollments")
async def list_enrollments(scope: Scope, group_id: uuid.UUID | None = None) -> list[dict]:
    stmt = select(Enrollment, Student, Group).join(
        Student, Student.id == Enrollment.student_id
    ).join(Group, Group.id == Enrollment.group_id).where(Enrollment.tenant_id == scope.ctx.id)
    if group_id:
        stmt = stmt.where(Enrollment.group_id == group_id)
    rows = (await scope.session.execute(stmt.limit(500))).all()
    return [
        {
            "id": str(enrollment.id),
            "student_id": str(student.id),
            "student": student.name,
            "group_id": str(group.id),
            "group": group.name,
            "status": enrollment.status,
            "price_month": float(enrollment.price_month),
            "start_date": enrollment.start_date,
        }
        for enrollment, student, group in rows
    ]


@router.post("/enrollments", status_code=201)
async def create_enrollment(body: EnrollIn, scope: Scope) -> dict:
    try:
        enrollment, invoice = await en.enroll(
            scope.session, scope.ctx,
            student_id=body.student_id,
            group_id=body.group_id,
            attribution="admin",
            start_date=body.start_date,
            discount_pct=body.discount_pct,
            idempotency_key=f"panel:{body.student_id}:{body.group_id}",
        )
    except en.SeatsFull as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "group is full") from exc
    except en.AlreadyEnrolled as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "already enrolled") from exc
    await schedule_enrollment_jobs(scope.session, scope.ctx, enrollment.id)
    return {
        "id": str(enrollment.id),
        "invoice_id": str(invoice.id) if invoice else None,
        "amount": float(invoice.amount) if invoice else None,
    }


@router.get("/invoices")
async def list_invoices(scope: Scope, open_only: bool = True) -> list[dict]:
    stmt = select(Invoice, Student).join(Student, Student.id == Invoice.student_id).where(
        Invoice.tenant_id == scope.ctx.id
    )
    if open_only:
        stmt = stmt.where(Invoice.status.in_(["open", "overdue"]))
    rows = (await scope.session.execute(stmt.order_by(Invoice.due_date).limit(500))).all()
    return [
        {
            "id": str(invoice.id),
            "student_id": str(student.id),
            "student": student.name,
            "period": invoice.period,
            "amount": float(invoice.amount),
            "currency": invoice.currency,
            "due_date": invoice.due_date,
            "status": invoice.status,
            "note": invoice.note,
        }
        for invoice, student in rows
    ]


@router.post("/invoices/{invoice_id}/paid")
async def mark_invoice_cash(invoice_id: uuid.UUID, scope: Scope) -> dict:
    """Owner marks a transfer or cash payment. Stops the debt ladder."""
    invoice = (
        await scope.session.execute(
            select(Invoice).where(Invoice.tenant_id == scope.ctx.id, Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown invoice")
    if invoice.status == "paid":
        return {"ok": True, "status": "paid"}
    payment = Payment(
        tenant_id=scope.ctx.id,
        provider="manual",
        amount=invoice.amount,
        currency=invoice.currency,
        status="created",
        attributed_to="cash",
        entity_ref=str(invoice.id),
        external_id=f"cash:{invoice.id}",
    )
    scope.session.add(payment)
    await scope.session.flush()
    paid = await mark_paid(scope.session, scope.ctx, payment.id, provider_ref="cash")
    if paid is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "payment was not recorded")
    await settle_payment(scope.session, scope.ctx, paid)
    return {"ok": True, "status": "paid", "payment_id": str(paid.id)}


@router.get("/lessons")
async def list_lessons(
    scope: Scope, day: date | None = None, group_id: uuid.UUID | None = None
) -> list[dict]:
    stmt = select(Lesson, Group).join(Group, Group.id == Lesson.group_id).where(
        Lesson.tenant_id == scope.ctx.id
    )
    if group_id:
        stmt = stmt.where(Lesson.group_id == group_id)
    if day:
        start = datetime.combine(day, datetime.min.time())
        stmt = stmt.where(Lesson.starts_at >= start, Lesson.starts_at < start + timedelta(days=1))
    rows = (await scope.session.execute(stmt.order_by(Lesson.starts_at).limit(200))).all()
    return [
        {
            "id": str(lesson.id),
            "group_id": str(group.id),
            "group": group.name,
            "starts_at": lesson.starts_at,
            "status": lesson.status,
            "topic": lesson.topic,
        }
        for lesson, group in rows
    ]


@router.post("/lessons/{lesson_id}/cancel")
async def cancel_lesson(lesson_id: uuid.UUID, body: LessonCancelIn, scope: Scope) -> dict:
    pair = await att.cancel_lesson(
        scope.session, scope.ctx, lesson_id, reason=body.reason, moved_to=body.moved_to
    )
    if pair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown lesson")
    from arix.scheduler import jobs

    await jobs.enqueue(
        scope.session, scope.ctx.id, "lesson_cancelled_notice", now_utc(),
        dedupe_key=jobs.dedupe("lesson_cancelled", lesson_id),
        payload={"lesson_id": str(lesson_id), "reason": body.reason or ""},
        timezone=scope.ctx.tz_name, respect_quiet_hours=False,
    )
    return {"ok": True, "status": pair[0].status}


@router.get("/trials")
async def list_trials(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(TrialLesson)
            .where(TrialLesson.tenant_id == scope.ctx.id, TrialLesson.active.is_(True))
            .order_by(TrialLesson.starts_at)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "starts_at": row.starts_at,
            "capacity": row.capacity,
            "course_id": str(row.course_id) if row.course_id else None,
            "group_id": str(row.group_id) if row.group_id else None,
        }
        for row in rows
    ]


@router.post("/attendance")
async def mark_attendance(body: AttendanceIn, scope: Scope) -> dict:
    outcome = await att.mark_attendance(
        scope.session, scope.ctx, body.lesson_id, body.absent_student_ids
    )
    if outcome.get("error"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, outcome["error"])
    return outcome


# ---------------------------------------------------------------- dialogs ---
@router.get("/conversations")
async def list_conversations(
    scope: Scope,
    state: str | None = None,
    assigned: str | None = None,
    limit: int = Query(default=50, le=200),
) -> list[dict]:
    stmt = (
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.tenant_id == scope.ctx.id)
        .order_by(Conversation.last_inbound_at.desc().nullslast())
        .limit(limit)
    )
    if state:
        stmt = stmt.where(Conversation.state == state)
    if assigned:
        stmt = stmt.where(Conversation.assigned_to == assigned)
    rows = (await scope.session.execute(stmt)).all()
    return [
        {
            "id": str(conversation.id),
            "channel": conversation.channel_type,
            "state": conversation.state,
            "assigned_to": conversation.assigned_to,
            "last_inbound_at": conversation.last_inbound_at,
            "contact": {
                "id": str(contact.id),
                "name": contact.name,
                "phone": contact.phone,
                "lang": contact.lang,
            },
        }
        for conversation, contact in rows
    ]


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: uuid.UUID, scope: Scope, limit: int = Query(default=100, le=500)
) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Message)
            .where(Message.tenant_id == scope.ctx.id, Message.conversation_id == conversation_id)
            .order_by(Message.seq)
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "direction": row.direction,
            "type": row.type,
            "text": row.stt_text or row.text,
            "sender": row.sender,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/conversations/{conversation_id}/reply")
async def manual_reply(conversation_id: uuid.UUID, text: str, scope: Scope) -> dict:
    from arix.domain.conversations import assign_to_human
    from arix.domain.messaging import send_message

    conversation = (
        await scope.session.execute(
            select(Conversation).where(
                Conversation.tenant_id == scope.ctx.id, Conversation.id == conversation_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown conversation")
    await assign_to_human(scope.session, conversation, None)
    outcome = await send_message(scope.session, scope.ctx, conversation, text, sender="admin")
    return {"ok": outcome.ok, "error": outcome.error or outcome.skipped_reason}


@router.post("/conversations/{conversation_id}/return-to-bot")
async def return_to_bot(conversation_id: uuid.UUID, scope: Scope) -> dict:
    from arix.domain.conversations import return_to_bot as _return

    conversation = (
        await scope.session.execute(
            select(Conversation).where(
                Conversation.tenant_id == scope.ctx.id, Conversation.id == conversation_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown conversation")
    await _return(scope.session, conversation)
    return {"ok": True}


@router.get("/handoffs")
async def list_handoffs(scope: Scope, open_only: bool = True) -> list[dict]:
    stmt = select(Handoff).where(Handoff.tenant_id == scope.ctx.id)
    if open_only:
        stmt = stmt.where(Handoff.resolved_at.is_(None))
    rows = (
        await scope.session.execute(stmt.order_by(Handoff.created_at.desc()).limit(100))
    ).scalars()
    return [
        {
            "id": str(row.id),
            "number": row.number,
            "reason": row.reason,
            "priority": row.priority,
            "summary": row.summary,
            "conversation_id": str(row.conversation_id),
            "created_at": row.created_at,
            "taken_at": row.taken_at,
            "resolved_at": row.resolved_at,
        }
        for row in rows
    ]


@router.get("/kb")
async def list_kb(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(KBItem).where(KBItem.tenant_id == scope.ctx.id).order_by(KBItem.type)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "type": row.type,
            "question": row.question,
            "answer": row.answer,
            "lang": row.lang,
            "verbatim": row.verbatim,
            "active": row.active,
        }
        for row in rows
    ]


@router.post("/kb", status_code=201)
async def create_kb(body: KBItemIn, scope: Scope) -> dict:
    row = KBItem(tenant_id=scope.ctx.id, **body.model_dump())
    scope.session.add(row)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.put("/kb/{item_id}")
async def update_kb(item_id: uuid.UUID, body: KBItemIn, scope: Scope) -> dict:
    row = (
        await scope.session.execute(
            select(KBItem).where(KBItem.tenant_id == scope.ctx.id, KBItem.id == item_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown kb item")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.delete("/kb/{item_id}", status_code=204)
async def delete_kb(item_id: uuid.UUID, scope: Scope) -> None:
    await scope.session.execute(
        delete(KBItem).where(KBItem.tenant_id == scope.ctx.id, KBItem.id == item_id)
    )


@router.get("/report/daily")
async def daily_report(scope: Scope, day: date | None = None) -> dict:
    report = await build_daily_report(scope.session, scope.ctx, day)
    return {"data": report.as_dict(), "text": render_daily_report(scope.ctx, report)}


@router.get("/stats/funnel")
async def funnel(scope: Scope, days: int = Query(default=7, ge=1, le=90)) -> dict:
    since = now_utc() - timedelta(days=days)
    rows = (
        await scope.session.execute(
            select(Event.type, func.count())
            .where(Event.tenant_id == scope.ctx.id, Event.created_at >= since)
            .group_by(Event.type)
        )
    ).all()
    counts = {event_type: count for event_type, count in rows}
    handoff_reasons = (
        await scope.session.execute(
            select(Handoff.reason, func.count())
            .where(Handoff.tenant_id == scope.ctx.id, Handoff.created_at >= since)
            .group_by(Handoff.reason)
        )
    ).all()
    metrics = (
        await scope.session.execute(
            select(DailyMetric)
            .where(DailyMetric.tenant_id == scope.ctx.id)
            .order_by(DailyMetric.day.desc())
            .limit(days)
        )
    ).scalars()
    debts = await bl.debt_summary(scope.session, scope.ctx)
    return {
        "window_days": days,
        "events": counts,
        "handoff_reasons": {reason: count for reason, count in handoff_reasons},
        "daily": [row.data for row in metrics],
        "debts": {"students": debts["students"], "total": debts["total"], "currency": debts["currency"]},
    }


__all__ = ["router"]
