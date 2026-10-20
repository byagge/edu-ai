"""Education-centre tools (ТЗ B4).

Prices, groups, schedules, balances and payment terms come only from here.
Two extra rules on top of the core ones:

* money and study reports are disclosed **only** to the paying guardian or to
  an adult learner — asking about someone else's child is a handoff;
* no promises about exam scores or admission, ever (checked again in
  ``postcheck``).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from arix.agent.toolkit import OutboundIntent, ToolContext, ToolDef, obj, tool
from arix.core.timeutil import humanize_local, local_date, to_tenant
from arix.templates import render

from . import billing as bl
from . import enrollment as en
from . import leveltest as lt
from . import trials as tr


def _as_uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _attribution(tc: ToolContext) -> str:
    scratch = tc.conversation.scratch or {}
    if scratch.get("attribution"):
        return str(scratch["attribution"])
    local = to_tenant(tc.now, tc.ctx.tz_name)
    quiet = tc.ctx.settings.quiet
    if quiet and quiet.contains_local(local):
        return "bot_night"
    return "bot"


@tool(
    "match_course",
    "Подобрать 1-3 курса по возрасту, цели, уровню и времени. Вызывай ВСЕГДА, когда клиент "
    "спрашивает про цену, направление или «что подойдёт». Называй только те цены, которые "
    "вернул инструмент.",
    obj(
        {
            "query": {"type": "string", "description": "Слова клиента: «английский ребёнку 10 лет»"},
            "age": {"type": "integer"},
            "direction": {
                "type": "string",
                "enum": ["lang", "exam", "it", "kids", "prof", "other"],
            },
            "level": {"type": "string"},
        }
    ),
    provides_prices=True,
)
async def match_course_tool(tc: ToolContext, args: dict) -> dict:
    query = args.get("query") or ""
    age = args.get("age")
    if age is None:
        age = en.parse_age(query)
    courses, matched = await en.list_courses(
        tc.session, tc.tenant_id,
        direction=args.get("direction"), query=query, age=age,
        limit=3, with_match_flag=True,
    )
    payload = []
    for course in courses:
        offers = await en.list_groups(tc.session, tc.ctx, course_id=course.id, limit=3)
        payload.append(
            {
                "course_id": str(course.id),
                "name": course.label(tc.lang),
                "direction": course.direction,
                "age_from": course.age_from,
                "age_to": course.age_to,
                "lessons_per_week": course.lessons_per_week,
                "lesson_min": course.lesson_min,
                "price_month": float(course.price_month) if course.price_month else None,
                "currency": course.currency,
                "has_trial": course.has_trial,
                "has_level_test": course.has_level_test,
                "groups": [
                    {
                        "group_id": str(offer.group_id),
                        "name": offer.name,
                        "level": offer.level,
                        "schedule": offer.schedule_text,
                        "price": en.price_text(offer),
                        "seats_left": offer.seats_left,
                        "starts": offer.start_date.isoformat() if offer.start_date else None,
                        "status": offer.status,
                    }
                    for offer in offers
                ],
            }
        )
    return {
        "count": len(payload),
        # False means nothing matched and this is just the catalogue — do NOT
        # quote these prices as the answer.
        "query_matched": matched,
        "age_used": age,
        "courses": payload,
        "note": "Курса по запросу нет — скажи, что уточнишь, и вызови handoff."
        if not matched
        else None,
    }


@tool(
    "list_groups",
    "Группы курса: расписание, уровень, цена, сколько мест свободно, дата старта.",
    obj({"course_id": {"type": "string"}, "level": {"type": "string"}}, ["course_id"]),
    provides_prices=True,
)
async def list_groups_tool(tc: ToolContext, args: dict) -> dict:
    course_id = _as_uuid(args.get("course_id"))
    if course_id is None:
        return {"error": "course_id_required", "groups": []}
    offers = await en.list_groups(
        tc.session, tc.ctx, course_id=course_id, level=args.get("level"), limit=6
    )
    tc.turn_state["group_offers"] = {str(o.group_id): o for o in offers}
    return {
        "count": len(offers),
        "groups": [
            {
                "group_id": str(offer.group_id),
                "name": offer.name,
                "course": offer.course_name,
                "level": offer.level,
                "teacher": offer.teacher,
                "schedule": offer.schedule_text,
                "price": en.price_text(offer),
                "seats_left": offer.seats_left,
                "starts": offer.start_date.isoformat() if offer.start_date else None,
                "status": offer.status,
            }
            for offer in offers
        ],
        "note": "Свободных групп нет — предложи add_to_waitlist." if not offers else None,
    }


@tool(
    "find_trials",
    "Свободные пробные занятия. Возвращает trial_key — его передавай в book_trial. "
    "Предлагай 2-3 конкретных даты, а не «когда вам удобно».",
    obj(
        {
            "course_id": {"type": "string"},
            "group_id": {"type": "string"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
        }
    ),
)
async def find_trials_tool(tc: ToolContext, args: dict) -> dict:
    course_id = _as_uuid(args.get("course_id"))
    offers = await tr.find_trials(
        tc.session, tc.ctx,
        course_id=course_id, group_id=_as_uuid(args.get("group_id")),
        date_from=_as_date(args.get("date_from")), limit=3, now=tc.now,
    )
    if not offers and course_id is not None and tc.ctx.settings.trial.free:
        # A brand-new centre may not have entered trial slots yet; create the
        # default grid so the first dialogue can still convert.
        await tr.ensure_trial_slots(tc.session, tc.ctx, course_id)
        offers = await tr.find_trials(
            tc.session, tc.ctx, course_id=course_id, limit=3, now=tc.now
        )
    return {
        "count": len(offers),
        "trials": [
            {
                "trial_key": offer.key,
                "when": humanize_local(offer.starts_at, tc.ctx.tz_name, tc.lang),
                "date": to_tenant(offer.starts_at, tc.ctx.tz_name).strftime("%Y-%m-%d"),
                "time": to_tenant(offer.starts_at, tc.ctx.tz_name).strftime("%H:%M"),
                "teacher": offer.teacher,
                "course": offer.course_name,
                "seats_left": offer.seats_left,
            }
            for offer in offers
        ],
    }


@tool(
    "save_student",
    "Сохранить ученика: имя и год рождения. Для ребёнка данные берём у родителя — "
    "контакт станет плательщиком. Вызывай перед book_trial и enroll.",
    obj(
        {
            "name": {"type": "string"},
            "birth_year": {"type": "integer"},
            "is_self": {"type": "boolean", "description": "true, если учится сам взрослый"},
            "level": {"type": "string"},
        },
        ["name"],
    ),
    mutating=True,
)
async def save_student_tool(tc: ToolContext, args: dict) -> dict:
    name = tc.vault.restore(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name_required"}
    student = await en.ensure_student(
        tc.session, tc.ctx,
        name=name, contact=tc.contact,
        birth_year=args.get("birth_year"), level=args.get("level"),
        is_self=bool(args.get("is_self")),
    )
    return {
        "ok": True,
        "student_id": str(student.id),
        "name": student.name,
        "level": student.level,
        "status": student.status,
    }


@tool(
    "book_trial",
    "Записать ученика на пробное занятие. Нужны trial_key из find_trials и student_id из "
    "save_student. Возвращает готовый текст подтверждения — отправь его как есть.",
    obj({"trial_key": {"type": "string"}, "student_id": {"type": "string"}},
        ["trial_key", "student_id"]),
    mutating=True,
)
async def book_trial_tool(tc: ToolContext, args: dict) -> dict:
    trial_id = _as_uuid(args.get("trial_key"))
    student_id = _as_uuid(args.get("student_id"))
    if trial_id is None or student_id is None:
        return {"ok": False, "error": "bad_arguments"}
    if tc.contact.consent_pd_at is None:
        return {
            "ok": False,
            "error": "consent_required",
            "hint": "Сначала получи согласие на обработку ПД и вызови save_contact_fields.",
        }
    try:
        booking = await tr.book_trial(
            tc.session, tc.ctx,
            trial_id=trial_id, student_id=student_id,
            attribution=_attribution(tc),
            idempotency_key=f"trial:{tc.conversation.id}:{trial_id}",
        )
    except tr.TrialFull:
        return {"ok": False, "error": "trial_full", "hint": "Вызови find_trials и предложи другое время."}
    except ValueError:
        return {"ok": False, "error": "unknown_trial"}

    refs = await tr.booking_with_refs(tc.session, tc.tenant_id, booking.id)
    if refs is None:
        return {"ok": False, "error": "unknown_trial"}
    _booking, trial, student, course = refs
    settings = tc.ctx.settings
    text = render(
        "trial_confirmed", tc.lang, overrides=settings.texts,
        student=student.name,
        course=course.label(tc.lang) if course else "занятие",
        when=humanize_local(trial.starts_at, tc.ctx.tz_name, tc.lang),
        address=settings.address or "уточните у администратора",
        map_line=f"Как добраться: {settings.map_url}\n" if settings.map_url else "",
        bring_line=settings.text("trial_bring", tc.lang, "Возьмите с собой только хорошее настроение.") + "\n",
    )
    tc.intents.append(OutboundIntent("trial_booked", {"booking_id": str(booking.id)}))
    return {
        "ok": True,
        "booking_id": str(booking.id),
        "confirmation_text": text,
        "instruction": "Отправь confirmation_text как есть, ничего не добавляя к цифрам.",
    }


@tool(
    "enroll",
    "Забронировать место в группе и выставить первый счёт. Нужны group_id и student_id. "
    "Возвращает сумму и срок оплаты — называй только их.",
    obj({"group_id": {"type": "string"}, "student_id": {"type": "string"}},
        ["group_id", "student_id"]),
    provides_prices=True,
    mutating=True,
)
async def enroll_tool(tc: ToolContext, args: dict) -> dict:
    group_id = _as_uuid(args.get("group_id"))
    student_id = _as_uuid(args.get("student_id"))
    if group_id is None or student_id is None:
        return {"ok": False, "error": "bad_arguments"}
    if tc.contact.consent_pd_at is None:
        return {"ok": False, "error": "consent_required"}
    try:
        enrollment, invoice = await en.enroll(
            tc.session, tc.ctx,
            student_id=student_id, group_id=group_id, attribution=_attribution(tc),
            idempotency_key=f"enroll:{tc.conversation.id}:{group_id}:{student_id}",
        )
    except en.SeatsFull:
        return {"ok": False, "error": "seats_full", "hint": "Предложи другую группу или add_to_waitlist."}
    except en.AlreadyEnrolled:
        return {"ok": False, "error": "already_enrolled"}
    except ValueError:
        return {"ok": False, "error": "unknown_group"}

    refs = await en.group_with_refs(tc.session, tc.tenant_id, group_id)
    group_name = refs[0].name if refs else ""
    course_name = refs[1].label(tc.lang) if refs else ""
    tc.intents.append(OutboundIntent("enrolled", {"enrollment_id": str(enrollment.id)}))
    return {
        "ok": True,
        "enrollment_id": str(enrollment.id),
        "group": group_name,
        "course": course_name,
        "schedule": en.schedule_text(refs[0].schedule) if refs else None,
        "invoice": (
            {
                "invoice_id": str(invoice.id),
                "amount": float(invoice.amount),
                "currency": invoice.currency,
                "due_date": invoice.due_date.isoformat(),
                "note": invoice.note,
            }
            if invoice
            else None
        ),
    }


@tool(
    "get_balance",
    "Счета и долги ученика. Доступно ТОЛЬКО родителю-плательщику или самому взрослому "
    "ученику: если ученик не связан с этим контактом, инструмент откажет — тогда вызови handoff.",
    obj({"student_id": {"type": "string"}}),
    provides_prices=True,
)
async def get_balance_tool(tc: ToolContext, args: dict) -> dict:
    allowed = await en.students_of_contact(tc.session, tc.tenant_id, tc.contact.id)
    if not allowed:
        return {"ok": False, "error": "no_linked_students"}
    requested = _as_uuid(args.get("student_id"))
    if requested is not None and requested not in {student.id for student in allowed}:
        # Someone asking about a learner they are not responsible for.
        tc.request_handoff(
            "other", "normal", "Спрашивает про ученика, не связанного с этим контактом"
        )
        return {"ok": False, "error": "not_your_student"}
    targets = [s for s in allowed if requested is None or s.id == requested]
    out = []
    for student in targets:
        balance = await bl.student_balance(tc.session, tc.ctx, student.id)
        out.append({"student_id": str(student.id), "name": student.name, **balance})
    return {"ok": True, "students": out}


@tool(
    "my_schedule",
    "Расписание, группы и ближайшие занятия учеников этого контакта.",
    obj({}),
)
async def my_schedule_tool(tc: ToolContext, args: dict) -> dict:
    students = await en.students_of_contact(tc.session, tc.tenant_id, tc.contact.id)
    out = []
    for student in students:
        groups = await en.student_groups(tc.session, tc.tenant_id, student.id)
        trials = await tr.upcoming_trials_for_student(tc.session, tc.tenant_id, student.id)
        out.append(
            {
                "student_id": str(student.id),
                "name": student.name,
                "level": student.level,
                "status": student.status,
                "groups": [
                    {
                        "group": group.name,
                        "course": course.label(tc.lang),
                        "schedule": en.schedule_text(group.schedule),
                        "status": enrollment.status,
                    }
                    for enrollment, group, course in groups
                ],
                "trials": [
                    {
                        "booking_id": str(booking.id),
                        "when": humanize_local(trial.starts_at, tc.ctx.tz_name, tc.lang),
                        "status": booking.status,
                    }
                    for booking, trial in trials
                ],
            }
        )
    return {"count": len(out), "students": out}


@tool(
    "start_level_test",
    "Начать тест уровня в чате. Возвращает первый вопрос и варианты — отправь их кнопками.",
    obj({"course_id": {"type": "string"}, "student_id": {"type": "string"}}),
    mutating=True,
)
async def start_level_test_tool(tc: ToolContext, args: dict) -> dict:
    if not tc.ctx.settings.features.level_test:
        return {"ok": False, "error": "level_test_disabled"}
    test = await lt.get_test(tc.session, tc.tenant_id, course_id=_as_uuid(args.get("course_id")))
    if test is None:
        return {"ok": False, "error": "no_test_configured"}
    run = await lt.start_test(
        tc.session, tc.ctx, test,
        contact_id=tc.contact.id, student_id=_as_uuid(args.get("student_id")),
    )
    question = lt.question_at(test, 0)
    tc.intents.append(
        OutboundIntent(
            "level_test_started",
            {"result_id": str(run.id), "total": lt.total_questions(test)},
        )
    )
    return {
        "ok": True,
        "result_id": str(run.id),
        "title": test.title,
        "progress": f"1/{lt.total_questions(test)}",
        "question": question.text if question else "",
        "options": question.options if question else [],
    }


@tool(
    "submit_answer",
    "Отправить ответ на вопрос теста уровня. Возвращает следующий вопрос или итоговый уровень.",
    obj({"result_id": {"type": "string"}, "option_index": {"type": "integer"}},
        ["result_id", "option_index"]),
    mutating=True,
)
async def submit_answer_tool(tc: ToolContext, args: dict) -> dict:
    result_id = _as_uuid(args.get("result_id"))
    if result_id is None:
        return {"ok": False, "error": "bad_result_id"}
    outcome = await lt.submit_answer(
        tc.session, tc.ctx, result_id, option_index=int(args.get("option_index") or 0)
    )
    return {"ok": "error" not in outcome, **outcome}


@tool(
    "add_to_waitlist",
    "Поставить в лист ожидания на курс или удобное время, если свободных групп нет.",
    obj(
        {
            "course_id": {"type": "string"},
            "student_id": {"type": "string"},
            "preferred_time": {"type": "string", "description": "HH:MM"},
            "note": {"type": "string"},
        },
        ["course_id"],
    ),
    mutating=True,
)
async def add_to_waitlist_tool(tc: ToolContext, args: dict) -> dict:
    from arix.core.timeutil import parse_hhmm

    from .models import Waitlist

    course_id = _as_uuid(args.get("course_id"))
    if course_id is None:
        return {"ok": False, "error": "course_id_required"}
    preferred = args.get("preferred_time")
    entry = Waitlist(
        tenant_id=tc.tenant_id,
        course_id=course_id,
        student_id=_as_uuid(args.get("student_id")),
        contact_id=tc.contact.id,
        preferred_time=parse_hhmm(preferred) if preferred else None,
        note=(args.get("note") or "")[:255] or None,
    )
    tc.session.add(entry)
    await tc.session.flush()
    return {"ok": True, "waitlist_id": str(entry.id)}


@tool(
    "cancel_trial",
    "Отменить запись на пробное занятие.",
    obj({"booking_id": {"type": "string"}}, ["booking_id"]),
    mutating=True,
)
async def cancel_trial_tool(tc: ToolContext, args: dict) -> dict:
    booking_id = _as_uuid(args.get("booking_id"))
    if booking_id is None:
        return {"ok": False, "error": "bad_booking_id"}
    allowed = await en.students_of_contact(tc.session, tc.tenant_id, tc.contact.id)
    refs = await tr.booking_with_refs(tc.session, tc.tenant_id, booking_id)
    if refs is None:
        return {"ok": False, "error": "unknown_booking"}
    booking, trial, student, _course = refs
    if student.id not in {item.id for item in allowed}:
        tc.request_handoff("other", "normal", "Отменяет пробное чужого ученика")
        return {"ok": False, "error": "not_your_student"}
    await tr.set_booking_status(tc.session, tc.ctx, booking_id, "cancelled")
    tc.intents.append(OutboundIntent("trial_cancelled", {"booking_id": str(booking_id)}))
    return {
        "ok": True,
        "text": render(
            "trial_cancelled", tc.lang, overrides=tc.ctx.settings.texts,
            when=humanize_local(trial.starts_at, tc.ctx.tz_name, tc.lang),
        ),
    }


@tool(
    "today",
    "Текущая дата в часовом поясе центра — чтобы правильно понимать «завтра» и «в субботу».",
    obj({}),
)
async def today_tool(tc: ToolContext, args: dict) -> dict:
    local = to_tenant(tc.now, tc.ctx.tz_name)
    return {
        "date": local_date(tc.now, tc.ctx.tz_name).isoformat(),
        "time": local.strftime("%H:%M"),
        "weekday": local.strftime("%A"),
    }


EDU_TOOLS: list[ToolDef] = [
    match_course_tool,
    list_groups_tool,
    find_trials_tool,
    save_student_tool,
    book_trial_tool,
    enroll_tool,
    get_balance_tool,
    my_schedule_tool,
    start_level_test_tool,
    submit_answer_tool,
    add_to_waitlist_tool,
    cancel_trial_tool,
    today_tool,
]

__all__ = ["EDU_TOOLS"]
