"""Education-centre implementation of the runtime's :class:`VerticalAdapter`."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.agent.runtime import ButtonOutcome, ConversationRuntime
from arix.agent.toolkit import ToolRegistry
from arix.agent.tools_core import CORE_TOOLS, PAYMENT_TOOLS
from arix.core.context import TenantContext
from arix.core.timeutil import humanize_local, now_utc
from arix.db.models_core import Contact, Conversation
from arix.domain.notify import notify_staff
from arix.logging_setup import get_logger

from . import attendance as att
from . import billing as bl
from . import enrollment as en
from . import trials as tr
from .playbook import EduPlaybook
from .scheduling import (
    cancel_trial_jobs,
    schedule_enrollment_jobs,
    schedule_invoice_ladder,
    schedule_trial_followup,
    schedule_trial_jobs,
    schedule_trial_rebook,
)
from .tools import EDU_TOOLS

log = get_logger("arix.edu")


class EduVertical:
    name = "edu"

    def playbook(self):
        return EduPlaybook()

    # ------------------------------------------------------------------
    async def handle_button(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        data: str,
        lang: str,
    ) -> ButtonOutcome | None:
        parts = data.split(":")
        head = parts[0]

        if head == "trial":
            return await self._trial_button(session, ctx, conversation, contact, parts, lang)
        if head == "tb":
            return await self._teacher_verdict(session, ctx, parts)
        if head == "at":
            return await self._attendance_button(session, ctx, parts)
        if head == "edu" and len(parts) >= 4 and parts[1] == "enroll":
            return await self._enroll_button(session, ctx, conversation, contact, parts, lang)
        if head == "lt" and len(parts) >= 3:
            # Level-test answer: handled deterministically, never by the model.
            from . import leveltest as lt

            outcome = await lt.submit_answer(
                session, ctx, uuid.UUID(parts[1]), option_index=int(parts[2])
            )
            if outcome.get("finished"):
                return ButtonOutcome(
                    reply=f"Готово: {outcome.get('score')} из {outcome.get('total')}, "
                    f"уровень {outcome.get('level') or 'базовый'}. Подобрать группу?",
                    label="level_test_done",
                )
            options = " / ".join(
                f"{index + 1}. {option}"
                for index, option in enumerate(outcome.get("options") or [])
            )
            return ButtonOutcome(
                reply=f"{outcome.get('progress')}\n{outcome.get('question')}\n{options}",
                label="level_test_next",
            )
        if head == "review" and len(parts) == 3:
            return await self._review_button(session, ctx, conversation, parts)
        return None

    async def _trial_button(
        self, session, ctx, conversation, contact, parts, lang
    ) -> ButtonOutcome | None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "book" and len(parts) >= 4:
            trial_id, student_id = uuid.UUID(parts[2]), uuid.UUID(parts[3])
            try:
                booking = await tr.book_trial(
                    session, ctx, trial_id=trial_id, student_id=student_id,
                    attribution="trial_followup",
                    idempotency_key=f"btn:{conversation.id}:{trial_id}:{student_id}",
                )
            except tr.TrialFull:
                return ButtonOutcome(
                    reply="Это время только что заняли. Подобрать другое?", label="trial_full"
                )
            except ValueError:
                return ButtonOutcome(reply="Это занятие уже недоступно.", label="unknown_trial")
            await schedule_trial_jobs(session, ctx, booking.id)
            refs = await tr.booking_with_refs(session, ctx.id, booking.id)
            when = humanize_local(refs[1].starts_at, ctx.tz_name, lang) if refs else ""
            return ButtonOutcome(reply=f"Записала на пробное {when}.", label="trial_booked")

        booking_id = uuid.UUID(parts[2]) if len(parts) > 2 else None
        if booking_id is None:
            return None
        if action == "confirm":
            await tr.set_booking_status(session, ctx, booking_id, "confirmed")
            return ButtonOutcome(reply="Спасибо, ждём вас!", label="trial_confirmed")
        if action == "cancel":
            await tr.set_booking_status(session, ctx, booking_id, "cancelled")
            await cancel_trial_jobs(session, ctx, booking_id)
            refs = await tr.booking_with_refs(session, ctx.id, booking_id)
            if refs:
                await notify_staff(
                    session, ctx, roles=["admin"],
                    text=f"🚫 Отмена пробного: {refs[2].name}",
                )
            return ButtonOutcome(
                reply="Отменила пробное. Подобрать другое время?", label="trial_cancelled"
            )
        if action == "move":
            scratch = dict(conversation.scratch or {})
            scratch["reschedule_booking_id"] = str(booking_id)
            scratch["offered_slots"] = []
            conversation.scratch = scratch
            await session.flush()
            # Fall through to the agent so it offers new times.
            return ButtonOutcome(handled=False, label="trial_move")
        return None

    async def _teacher_verdict(self, session, ctx, parts) -> ButtonOutcome | None:
        """The teacher's one-tap verdict after a trial (ТЗ B5 У4)."""
        action = parts[1] if len(parts) > 1 else ""
        if len(parts) < 3:
            return None
        booking_id = uuid.UUID(parts[2])
        if action == "noshow":
            await tr.set_booking_status(session, ctx, booking_id, "no_show")
            await cancel_trial_jobs(session, ctx, booking_id)
            await schedule_trial_rebook(session, ctx, booking_id)
            from arix.core.events import TRIAL_NO_SHOW, log_event

            await log_event(session, ctx.id, TRIAL_NO_SHOW, entity_ref=str(booking_id))
            return ButtonOutcome(
                reply="Отметила: не пришёл. Бот предложит новую дату через час.",
                label="trial_no_show",
            )
        if action == "score" and len(parts) >= 4:
            score = int(parts[3])
            booking = await tr.set_booking_status(
                session, ctx, booking_id, "attended", feedback_score=score
            )
            if booking is None:
                return None
            group = await tr.recommend_group(session, ctx, booking)
            if group is not None:
                booking.recommended_group_id = group.id
                await session.flush()
            await schedule_trial_followup(session, ctx, booking_id)
            from arix.core.events import TRIAL_ATTENDED, log_event

            await log_event(
                session, ctx.id, TRIAL_ATTENDED, entity_ref=str(booking_id),
                data={"score": score},
            )
            return ButtonOutcome(
                reply=f"Спасибо, оценка {score} записана. Родителю уйдёт предложение группы.",
                label="trial_scored",
            )
        return None

    async def _attendance_button(self, session, ctx, parts) -> ButtonOutcome | None:
        """Teacher marks who was missing; everyone else is present."""
        action = parts[1] if len(parts) > 1 else ""
        if len(parts) < 3:
            return None
        lesson_id = uuid.UUID(parts[2])
        if action == "absent" and len(parts) >= 4:
            student_id = uuid.UUID(parts[3])
            scratch_key = f"absent:{lesson_id}"
            pending = await self._toggle_absent(session, ctx, scratch_key, student_id)
            return ButtonOutcome(
                reply=f"Отмечено отсутствующих: {len(pending)}. "
                "Нажмите «Все отмечены», когда закончите.",
                label="attendance_mark",
            )
        if action == "done":
            scratch_key = f"absent:{lesson_id}"
            absent = [uuid.UUID(value) for value in self._pending.get(scratch_key, [])]
            outcome = await att.mark_attendance(session, ctx, lesson_id, absent)
            self._pending.pop(scratch_key, None)
            if absent:
                from arix.scheduler import jobs

                await jobs.enqueue(
                    session, ctx.id, "absence_notice", now_utc(),
                    dedupe_key=jobs.dedupe("absence_notice", lesson_id),
                    payload={"lesson_id": str(lesson_id)},
                    timezone=ctx.tz_name, respect_quiet_hours=False,
                )
            return ButtonOutcome(
                reply=f"Готово: {outcome.get('marked', 0)} учеников, "
                f"отсутствовали {outcome.get('absent', 0)}.",
                label="attendance_done",
            )
        return None

    #: Per-process buffer of "who is absent" while the teacher is tapping.
    #: It is intentionally in-memory: the authoritative record is written in
    #: one go by ``mark_attendance`` when the teacher presses "done", and a
    #: lost buffer only means re-tapping, never a wrong attendance record.
    _pending: dict[str, list[str]] = {}

    async def _toggle_absent(self, session, ctx, key: str, student_id: uuid.UUID) -> list[str]:
        current = self._pending.setdefault(key, [])
        value = str(student_id)
        if value in current:
            current.remove(value)
        else:
            current.append(value)
        return current

    async def _enroll_button(
        self, session, ctx, conversation, contact, parts, lang
    ) -> ButtonOutcome | None:
        group_id, student_id = uuid.UUID(parts[2]), uuid.UUID(parts[3])
        if contact.consent_pd_at is None:
            from arix.agent.scripted import ask_for

            return ButtonOutcome(reply=ask_for(["consent_pd"]), label="need_consent")
        try:
            enrollment, invoice = await en.enroll(
                session, ctx,
                student_id=student_id, group_id=group_id, attribution="trial_followup",
                idempotency_key=f"btn:{conversation.id}:{group_id}:{student_id}",
            )
        except en.SeatsFull:
            return ButtonOutcome(
                reply="Мест в этой группе уже нет. Подобрать другую?", label="seats_full"
            )
        except en.AlreadyEnrolled:
            return ButtonOutcome(reply="Ученик уже в этой группе.", label="already_enrolled")
        except ValueError:
            return ButtonOutcome(reply="Эта группа недоступна.", label="unknown_group")

        await self.on_conversion(
            session, ctx, conversation, "enrolled", {"enrollment_id": str(enrollment.id)}
        )
        refs = await en.group_with_refs(session, ctx.id, group_id)
        lines = [f"Записала в группу «{refs[0].name}»." if refs else "Записала в группу."]
        if refs:
            lines.append(f"Расписание: {en.schedule_text(refs[0].schedule)}")
        if invoice:
            lines.append(
                f"Счёт за {invoice.period}: {int(invoice.amount):,}".replace(",", " ")
                + f" {invoice.currency}, оплатить до {invoice.due_date:%d.%m}."
            )
        return ButtonOutcome(reply="\n".join(lines), label="enrolled")

    async def _review_button(self, session, ctx, conversation, parts) -> ButtonOutcome | None:
        enrollment_id, score = uuid.UUID(parts[1]), int(parts[2])
        settings = ctx.settings
        if score >= settings.reviews.promote_from_score:
            link = settings.reviews.links.get("2gis") or settings.reviews.links.get("google")
            reply = (
                f"Спасибо! Если не сложно, оставьте пару слов здесь — это очень помогает: {link}"
                if link
                else "Спасибо за оценку!"
            )
        elif score <= settings.reviews.alert_up_to_score:
            await notify_staff(
                session, ctx, roles=["owner", "admin"],
                text=f"⚠️ Низкая оценка обучения: {score}★ (enrollment {enrollment_id})",
            )
            reply = "Спасибо за честность. Передам руководителю — с вами свяжутся."
        else:
            reply = "Спасибо, учтём!"
        from arix.core.events import REVIEW_RECEIVED, log_event

        await log_event(
            session, ctx.id, REVIEW_RECEIVED,
            conversation_id=conversation.id, entity_ref=str(enrollment_id),
            data={"score": score},
        )
        return ButtonOutcome(reply=reply, label="review_recorded")

    # ------------------------------------------------------------------
    async def on_conversion(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        kind: str,
        payload: dict,
    ) -> None:
        if kind == "trial_booked":
            booking_id = _uuid(payload.get("booking_id"))
            if booking_id is None:
                return
            await schedule_trial_jobs(session, ctx, booking_id)
            refs = await tr.booking_with_refs(session, ctx.id, booking_id)
            if refs:
                _booking, trial, student, course = refs
                await notify_staff(
                    session, ctx, roles=["admin"],
                    text=(
                        f"✅ Пробное записано\n{student.name}"
                        + (f" · {course.label('ru')}" if course else "")
                        + f"\n{humanize_local(trial.starts_at, ctx.tz_name, 'ru')}"
                    ),
                )
            return

        if kind == "trial_cancelled":
            booking_id = _uuid(payload.get("booking_id"))
            if booking_id is not None:
                await cancel_trial_jobs(session, ctx, booking_id)
            return

        if kind == "enrolled":
            enrollment_id = _uuid(payload.get("enrollment_id"))
            if enrollment_id is None:
                return
            await schedule_enrollment_jobs(session, ctx, enrollment_id)
            from .models import Enrollment, Student

            enrollment = (
                await session.execute(
                    select(Enrollment).where(Enrollment.id == enrollment_id)
                )
            ).scalar_one_or_none()
            if enrollment is None:
                return
            # Stop the trial follow-up ladder: the money arrived.
            from arix.scheduler import jobs

            await jobs.cancel(session, ctx.id, dedupe_prefix="trial_followup:")
            invoices = await bl.open_invoices(
                session, ctx.id, student_id=enrollment.student_id
            )
            for invoice in invoices:
                await schedule_invoice_ladder(session, ctx, invoice)
            student = (
                await session.execute(select(Student).where(Student.id == enrollment.student_id))
            ).scalar_one()
            refs = await en.group_with_refs(session, ctx.id, enrollment.group_id)
            await notify_staff(
                session, ctx, roles=["admin"],
                text=(
                    f"🎓 Зачисление\n{student.name}"
                    + (f"\nГруппа: {refs[0].name}" if refs else "")
                    + f"\nИсточник: {enrollment.attribution}"
                ),
            )


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.extend(CORE_TOOLS)
    registry.extend(EDU_TOOLS)
    registry.extend(PAYMENT_TOOLS)
    return registry


def build_runtime(brain=None) -> ConversationRuntime:
    return ConversationRuntime(build_registry(), EduVertical(), brain=brain)


def _uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


__all__ = ["EduVertical", "build_registry", "build_runtime"]
