"""Placement test in the chat (ТЗ B5 У2).

12–15 button questions, 5–7 minutes, then a level, a matching group and a
trial.  The scoring table lives in the tenant's data, not in code, so a centre
can change its own thresholds.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import LEVEL_TEST_DONE, log_event
from arix.core.timeutil import now_utc

from .models import LevelTest, Student, TestResult


@dataclass(frozen=True)
class Question:
    index: int
    text: str
    options: list[str]


def level_for(score: int, scoring: dict) -> str | None:
    """Map a score to a level using the tenant's own table."""
    bands = (scoring or {}).get("bands") or []
    for band in bands:
        if int(band.get("min", 0)) <= score <= int(band.get("max", 999)):
            return band.get("level")
    return None


def question_at(test: LevelTest, index: int) -> Question | None:
    items = (test.questions or {}).get("items") or []
    if index < 0 or index >= len(items):
        return None
    item = items[index]
    return Question(index=index, text=item.get("q", ""), options=list(item.get("options") or []))


def total_questions(test: LevelTest) -> int:
    return len((test.questions or {}).get("items") or [])


def score_answers(test: LevelTest, answers: dict[str, int]) -> int:
    items = (test.questions or {}).get("items") or []
    score = 0
    for position, item in enumerate(items):
        given = answers.get(str(position))
        if given is None:
            continue
        if int(given) == int(item.get("answer", -1)):
            score += 1
    return score


async def get_test(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    course_id: uuid.UUID | None = None,
    code: str | None = None,
) -> LevelTest | None:
    stmt = select(LevelTest).where(LevelTest.tenant_id == tenant_id, LevelTest.active.is_(True))
    if code:
        stmt = stmt.where(LevelTest.code == code)
    elif course_id:
        stmt = stmt.where(LevelTest.course_id == course_id)
    found = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if found is not None or (code or course_id) is None:
        return found
    # Fall back to any active test: a centre usually has one.
    return (
        await session.execute(
            select(LevelTest)
            .where(LevelTest.tenant_id == tenant_id, LevelTest.active.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()


async def start_test(
    session: AsyncSession,
    ctx: TenantContext,
    test: LevelTest,
    *,
    contact_id: uuid.UUID,
    student_id: uuid.UUID | None = None,
) -> TestResult:
    result = TestResult(
        tenant_id=ctx.id,
        student_id=student_id,
        contact_id=contact_id,
        test_id=test.id,
        score=0,
        answers={"given": {}, "cursor": 0},
    )
    session.add(result)
    await session.flush()
    return result


async def submit_answer(
    session: AsyncSession,
    ctx: TenantContext,
    result_id: uuid.UUID,
    *,
    option_index: int,
) -> dict:
    """Record one answer and return either the next question or the verdict."""
    result = (
        await session.execute(
            select(TestResult).where(
                TestResult.tenant_id == ctx.id, TestResult.id == result_id
            )
        )
    ).scalar_one_or_none()
    if result is None:
        return {"error": "unknown_test_run"}
    if result.finished_at is not None:
        return {"finished": True, "score": result.score, "level": result.level}

    test = (
        await session.execute(select(LevelTest).where(LevelTest.id == result.test_id))
    ).scalar_one()
    state = dict(result.answers or {})
    given = dict(state.get("given") or {})
    cursor = int(state.get("cursor") or 0)

    given[str(cursor)] = int(option_index)
    cursor += 1
    result.answers = {"given": given, "cursor": cursor}

    if cursor < total_questions(test):
        question = question_at(test, cursor)
        await session.flush()
        return {
            "finished": False,
            "progress": f"{cursor + 1}/{total_questions(test)}",
            "question": question.text if question else "",
            "options": question.options if question else [],
        }

    score = score_answers(test, given)
    result.score = score
    result.level = level_for(score, test.scoring)
    result.finished_at = now_utc()
    if result.student_id:
        student = (
            await session.execute(select(Student).where(Student.id == result.student_id))
        ).scalar_one_or_none()
        if student is not None and result.level:
            student.level = result.level
    await session.flush()

    await log_event(
        session, ctx.id, LEVEL_TEST_DONE,
        contact_id=result.contact_id, entity_ref=str(result.id),
        data={"score": score, "level": result.level, "test": test.code},
    )
    return {
        "finished": True,
        "score": score,
        "total": total_questions(test),
        "level": result.level,
    }


async def active_run(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID
) -> TestResult | None:
    return (
        await session.execute(
            select(TestResult)
            .where(
                TestResult.tenant_id == tenant_id,
                TestResult.contact_id == contact_id,
                TestResult.finished_at.is_(None),
            )
            .order_by(TestResult.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def last_result(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID
) -> TestResult | None:
    return (
        await session.execute(
            select(TestResult)
            .where(
                TestResult.tenant_id == tenant_id,
                TestResult.contact_id == contact_id,
                TestResult.finished_at.isnot(None),
            )
            .order_by(TestResult.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


__all__ = [
    "Question",
    "active_run",
    "get_test",
    "last_result",
    "level_for",
    "question_at",
    "score_answers",
    "start_test",
    "submit_answer",
    "total_questions",
]
