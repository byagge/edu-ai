"""Deterministic dialogue flow for an education centre.

Mirrors the acceptance scenarios of ТЗ B5: request → course match → level test
or trial → booking → follow-up → balance and debts.  It speaks plainly and
never states a number that did not come from a tool.
"""
from __future__ import annotations

from arix.agent.guardrails import Intent
from arix.agent.scripted import (
    NO,
    YES,
    Reply,
    Turn,
    collect_contact_fields,
    has_any,
    match_offered,
)

BOOK_WORDS = ("запиш", "записаться", "хочу запис", "пробн", "пробное", "trial", "navbat", "жазыл")
TEST_WORDS = ("тест", "уровень", "определить уровень", "level test", "деңгей", "daraja")
BALANCE_WORDS = ("баланс", "долг", "задолж", "сколько должен", "сколько я должен", "оплата",
                 "оплатить", "счёт", "счет", "квитанц", "qarz", "to'lov")
SCHEDULE_WORDS = ("расписание", "когда занятия", "мои группы", "график", "когда урок",
                  "dars jadvali", "кесте")
CANCEL_WORDS = ("отмен", "не придём", "не придем", "не приду", "bekor")
GROUPS_WORDS = ("групп", "какие группы", "свободные места", "места есть", "guruh")
PRICE_WORDS = ("сколько стоит", "сколько стоят", "цена", "цены", "стоимость", "почем", "почём",
               "narxi", "qancha", "баға", "қанша тұрады")
#: Questions *about* the centre rather than about a course.
KB_FIRST_WORDS = ("как проходит", "сколько длится", "можно ли", "условия", "возврат",
                  "рассрочк", "документ", "где вы", "адрес", "как добраться", "парковк",
                  "часы работы", "во сколько вы", "работаете ли", "какие преподавател",
                  "пробное бесплатн", "что взять", "как оплатить", "способы оплаты")
GUARANTEE_WORDS = ("гарантиру", "гарантия", "точно сдам", "точно поступ", "обещаете",
                   "kafolat", "кепілдік")


class EduPlaybook:
    """Vertical flow handed to :class:`arix.agent.scripted.ScriptedBrain`."""

    async def respond(self, turn: Turn) -> Reply | None:
        state = turn.state
        lower = turn.lower

        # --- promises about scores and admission are never ours to make ---
        if has_any(lower, GUARANTEE_WORDS):
            await turn.call(
                "handoff",
                {"reason": "out_of_kb", "priority": "normal",
                 "summary": "Спрашивает гарантии по баллам или поступлению"},
            )
            return Reply(
                "Гарантировать баллы или поступление я не могу — это зависит от самого "
                "ученика. Расскажу программу и часы, а подробнее ответит администратор."
            )

        # --- a level test in progress owns the dialogue --------------------
        if state.get("level_test_run_id"):
            reply = await self._continue_test(turn)
            if reply is not None:
                return reply

        # --- a pending trial booking needs name, phone and consent --------
        if state.get("pending_trial_key"):
            reply = await self._finish_trial(turn)
            if reply is not None:
                return reply

        # --- the client picked one of the offered trial times -------------
        offered = state.get("offered_slots") or []
        if offered:
            chosen = match_offered(lower, offered)
            if chosen is not None:
                return await self._take_trial(turn, chosen)
            if lower in NO:
                return Reply("Хорошо. Если передумаете — напишите, подберу другое время.")

        # --- explicit intents --------------------------------------------
        if has_any(lower, BALANCE_WORDS):
            return await self._balance(turn)

        if has_any(lower, SCHEDULE_WORDS):
            return await self._schedule(turn)

        if has_any(lower, CANCEL_WORDS) and not has_any(lower, ("можно ли", "условия")):
            return await self._cancel(turn)

        if has_any(lower, TEST_WORDS):
            return await self._start_test(turn)

        # --- questions about the centre go to the base first --------------
        if has_any(lower, KB_FIRST_WORDS) and not has_any(lower, PRICE_WORDS):
            kb = await turn.call("kb_search", {"query": turn.text, "lang": turn.lang})
            facts = kb.get("facts") or []
            if facts:
                return Reply(facts[0]["answer"])

        if turn.pre.has(Intent.DIAGNOSIS):
            # Medical wording in an education centre: nothing for us to answer.
            await turn.call(
                "handoff",
                {"reason": "out_of_kb", "priority": "normal", "summary": "Вопрос не про обучение"},
            )
            return Reply("Это не по моей части — передаю администратору, он ответит здесь.")

        # --- the course / group / knowledge-base cascade ------------------
        return await self._course_flow(turn)

    # ------------------------------------------------------------------
    async def _course_flow(self, turn: Turn) -> Reply | None:
        """Course match → group → trial, falling back to the base and a human."""
        matched = await turn.call("match_course", {"query": turn.text})
        courses = matched.get("courses") or []
        if not courses or not matched.get("query_matched", True):
            # Quoting the first course here would be exactly the invented price
            # the brief forbids, so try the base and then hand over.
            kb = await turn.call("kb_search", {"query": turn.text, "lang": turn.lang})
            facts = kb.get("facts") or []
            if facts:
                return Reply(facts[0]["answer"])
            await turn.call(
                "handoff",
                {"reason": "out_of_kb", "priority": "normal",
                 "summary": f"Нет курса или ответа по запросу: {turn.text[:80]}"},
            )
            return Reply("Такого курса в программе не нашла — уточню у администратора и вернусь.")

        course = courses[0]
        groups = course.get("groups") or []
        head = _course_line(course)

        if groups:
            group = groups[0]
            head += (
                f" Группа «{group['name']}»: {group['schedule']}, {group['price']}"
                + (f", осталось мест {group['seats_left']}" if group["seats_left"] <= 3 else "")
                + "."
            )
        else:
            await turn.call(
                "add_to_waitlist",
                {"course_id": course["course_id"], "note": turn.text[:200]},
            )
            return Reply(
                f"{head} Свободных групп сейчас нет — записала вас в лист ожидания, "
                "сообщу, как откроется набор."
            )

        trials = await turn.call("find_trials", {"course_id": course["course_id"]})
        items = trials.get("trials") or []
        if not items:
            return Reply(
                f"{head} Пробное на этой неделе уже занято — подобрать на следующую?"
            )
        turn.offer(items)
        when = ", ".join(item["when"] for item in items)
        tail = "Пробное бесплатное." if turn.state.get("trial_free", True) else ""
        if course.get("has_level_test"):
            # One question per message, so the test is an alternative, not a
            # second question: the client picks a time or asks for the test.
            tail += " Можно начать с теста уровня — напишите «тест»."
        return Reply(f"{head} Записать на пробное? Есть {when}. {tail}".strip())

    async def _take_trial(self, turn: Turn, chosen: dict) -> Reply:
        """The client picked a trial time: remember it and collect what we lack."""
        scratch_needed = _missing_contact_fields(turn.state)
        turn.state["pending_trial_key"] = chosen["trial_key"]
        turn.state["still_needed"] = scratch_needed
        turn.offer([], pending_trial_key=chosen["trial_key"], still_needed=scratch_needed)
        if scratch_needed:
            from arix.agent.scripted import ask_for

            return Reply(ask_for(scratch_needed))
        return await self._confirm_trial(turn, chosen["trial_key"])

    async def _finish_trial(self, turn: Turn) -> Reply | None:
        needed = list(turn.state.get("still_needed") or [])
        if needed:
            reply = await collect_contact_fields(turn, needed)
            if reply is not None:
                return reply
        # Everything known: who is the student?
        student_name = turn.state.get("student_name") or turn.state.get("contact_name")
        saved = await turn.call(
            "save_student",
            {"name": student_name or turn.text.strip(), "is_self": not turn.state.get("is_parent")},
        )
        if not saved.get("ok"):
            return Reply("Подскажите, пожалуйста, имя ученика?")
        return await self._confirm_trial(turn, turn.state["pending_trial_key"], saved["student_id"])

    async def _confirm_trial(
        self, turn: Turn, trial_key: str, student_id: str | None = None
    ) -> Reply:
        if student_id is None:
            name = turn.state.get("student_name") or turn.state.get("contact_name") or "Ученик"
            saved = await turn.call(
                "save_student", {"name": name, "is_self": not turn.state.get("is_parent")}
            )
            if not saved.get("ok"):
                return Reply("Подскажите, пожалуйста, имя ученика?")
            student_id = saved["student_id"]

        booked = await turn.call(
            "book_trial", {"trial_key": trial_key, "student_id": student_id}
        )
        turn.offer([], pending_trial_key=None, still_needed=[])
        if booked.get("ok"):
            return Reply(booked["confirmation_text"])
        if booked.get("error") == "trial_full":
            return Reply("Это время только что заняли. Подобрать другое?")
        if booked.get("error") == "consent_required":
            from arix.agent.scripted import ask_for

            return Reply(ask_for(["consent_pd"]))
        await turn.call(
            "handoff",
            {"reason": "other", "priority": "normal", "summary": "Не удалось записать на пробное"},
        )
        return Reply("Передаю администратору — он запишет на пробное.")

    async def _start_test(self, turn: Turn) -> Reply:
        started = await turn.call("start_level_test", {})
        if not started.get("ok"):
            # No test configured: fall back to a trial, which converts anyway.
            return await self._course_flow(turn) or Reply(
                "Теста уровня пока нет — предлагаю прийти на пробное занятие."
            )
        turn.offer([], level_test_run_id=started["result_id"])
        options = " / ".join(
            f"{index + 1}. {option}" for index, option in enumerate(started["options"])
        )
        return Reply(
            f"{started['title']} — {started['progress']}\n{started['question']}\n{options}"
        )

    async def _continue_test(self, turn: Turn) -> Reply | None:
        index = _option_index(turn.lower)
        if index is None:
            return None
        outcome = await turn.call(
            "submit_answer",
            {"result_id": turn.state["level_test_run_id"], "option_index": index},
        )
        if not outcome.get("ok"):
            turn.offer([], level_test_run_id=None)
            return Reply("Тест прервался. Начнём заново или сразу запишу на пробное?")
        if not outcome.get("finished"):
            options = " / ".join(
                f"{position + 1}. {option}"
                for position, option in enumerate(outcome.get("options") or [])
            )
            return Reply(f"{outcome['progress']}\n{outcome['question']}\n{options}")

        turn.offer([], level_test_run_id=None)
        level = outcome.get("level") or "базовый"
        matched = await turn.call("match_course", {"query": turn.text, "level": level})
        courses = matched.get("courses") or []
        head = (
            f"Готово: {outcome['score']} из {outcome['total']}, уровень {level}."
        )
        if not courses:
            await turn.call(
                "handoff",
                {"reason": "other", "priority": "normal",
                 "summary": f"Тест пройден, уровень {level}, нужна группа"},
            )
            return Reply(f"{head} Передаю администратору — подберёт группу под ваш уровень.")
        trials = await turn.call("find_trials", {"course_id": courses[0]["course_id"]})
        items = trials.get("trials") or []
        if items:
            turn.offer(items)
            when = ", ".join(item["when"] for item in items)
            return Reply(f"{head} Подходит {_course_line(courses[0])} Записать на пробное? Есть {when}.")
        return Reply(f"{head} Подходит {_course_line(courses[0])}")

    async def _balance(self, turn: Turn) -> Reply:
        balance = await turn.call("get_balance", {})
        if not balance.get("ok"):
            if balance.get("error") == "no_linked_students":
                return Reply(
                    "Пока не вижу ученика за этим номером. Подскажите имя ученика — найду счёт."
                )
            return Reply("Эти данные покажет администратор — передаю ему.")
        lines: list[str] = []
        for student in balance["students"]:
            if not student["invoices"]:
                lines.append(f"{student['name']}: задолженности нет.")
                continue
            parts = [
                f"{invoice['period']} — {_money(invoice['amount'])} "
                f"{_symbol(student['currency'])}, до {invoice['due_date'][8:10]}."
                f"{invoice['due_date'][5:7]}"
                + (f" (просрочка {invoice['days_overdue']} дн.)" if invoice["days_overdue"] else "")
                for invoice in student["invoices"]
            ]
            lines.append(
                f"{student['name']}: {_money(student['debt'])} "
                f"{_symbol(student['currency'])}\n  " + "\n  ".join(parts)
            )
        return Reply("\n".join(lines))

    async def _schedule(self, turn: Turn) -> Reply:
        schedule = await turn.call("my_schedule", {})
        students = schedule.get("students") or []
        if not students:
            return Reply("Активных занятий не нашла. Подобрать курс и записать на пробное?")
        lines: list[str] = []
        for student in students:
            head = f"{student['name']}"
            if student.get("level"):
                head += f" · уровень {student['level']}"
            lines.append(head)
            for group in student["groups"]:
                lines.append(f"  {group['course']} — {group['group']}, {group['schedule']}")
            for trial in student["trials"]:
                lines.append(f"  пробное: {trial['when']}")
            if not student["groups"] and not student["trials"]:
                lines.append("  занятий пока нет")
        return Reply("\n".join(lines))

    async def _cancel(self, turn: Turn) -> Reply:
        schedule = await turn.call("my_schedule", {})
        for student in schedule.get("students") or []:
            for trial in student.get("trials") or []:
                out = await turn.call("cancel_trial", {"booking_id": trial["booking_id"]})
                if out.get("ok"):
                    return Reply(out["text"])
        await turn.call(
            "handoff",
            {"reason": "other", "priority": "normal", "summary": "Хочет отменить занятия"},
        )
        return Reply("Передаю администратору — он всё отменит и подскажет по условиям.")


def _course_line(course: dict) -> str:
    price = (
        f"{_money(course['price_month'])} {_symbol(course['currency'])}/мес"
        if course.get("price_month")
        else "цена уточняется"
    )
    ages = (
        f", {course['age_from']}–{course['age_to']} лет"
        if course.get("age_from") and course.get("age_to")
        else ""
    )
    return (
        f"{course['name']}{ages}: {price}, "
        f"{course['lessons_per_week']} раза в неделю по {course['lesson_min']} мин."
    )


def _missing_contact_fields(state: dict) -> list[str]:
    return [
        field
        for field, known in (
            ("name", state.get("has_name")),
            ("phone", state.get("has_phone")),
            ("consent_pd", state.get("consent_pd")),
        )
        if not known
    ]


def _option_index(lower: str) -> int | None:
    """Map "2", "б", "вариант 3" to a zero-based option index."""
    import re

    match = re.search(r"\b([1-9])\b", lower)
    if match:
        return int(match.group(1)) - 1
    letters = {"а": 0, "a": 0, "б": 1, "b": 1, "в": 2, "c": 2, "г": 3, "d": 3}
    for letter, index in letters.items():
        if lower.strip() == letter:
            return index
    if lower.strip() in YES:
        return 0
    return None


def _money(value: float | None) -> str:
    if not value:
        return "—"
    return f"{int(value):,}".replace(",", " ")


def _symbol(currency: str) -> str:
    return {"KZT": "₸", "UZS": "сум", "RUB": "₽"}.get(currency, currency)


__all__ = ["EduPlaybook"]
