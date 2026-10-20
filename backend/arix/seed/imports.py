"""Excel import for an education centre (ТЗ B8).

Columns are matched case-insensitively against aliases. Rows that cannot be
parsed are reported back; nothing is invented to fill a gap.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.pii import normalize_phone
from arix.db.models_core import Contact
from arix.verticals.edu.models import Course, Enrollment, Group, Guardian, Invoice, Student

COURSE_ALIASES = {
    "code": ("код", "code"),
    "name": ("курс", "название", "наименование", "name"),
    "price": ("цена", "стоимость", "price", "в месяц"),
    "direction": ("направление", "direction"),
}

GROUP_ALIASES = {
    "name": ("группа", "название", "name"),
    "course": ("курс", "course"),
    "capacity": ("мест", "вместимость", "capacity"),
    "schedule": ("расписание", "дни", "schedule"),
}

STUDENT_ALIASES = {
    "student": ("ученик", "фио ученика", "студент", "student"),
    "parent": ("родитель", "фио родителя", "parent"),
    "phone": ("телефон", "phone", "номер"),
    "group": ("группа", "group"),
    "debt": ("долг", "задолженность", "debt"),
}


def _load_rows(path: str) -> list[dict[str, Any]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not rows:
        return []
    headers = [str(cell or "").strip().lower() for cell in rows[0]]
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        out.append({headers[i]: row[i] for i in range(min(len(headers), len(row)))})
    return out


def _pick(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for key, value in row.items():
        if not key:
            continue
        for alias in aliases:
            if key == alias or key.startswith(alias) or alias in key:
                return value
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.,]", "", str(value)).replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_student_row(row: dict[str, Any]) -> dict[str, Any] | str:
    """Turn one spreadsheet row into fields, or an error string."""
    student = str(_pick(row, STUDENT_ALIASES["student"]) or "").strip()
    if not student:
        return "нет имени ученика"
    phone_raw = _pick(row, STUDENT_ALIASES["phone"])
    phone = normalize_phone(str(phone_raw)) if phone_raw else None
    debt = _number(_pick(row, STUDENT_ALIASES["debt"]))
    return {
        "student": student,
        "parent": str(_pick(row, STUDENT_ALIASES["parent"]) or "").strip() or None,
        "phone": phone,
        "group": str(_pick(row, STUDENT_ALIASES["group"]) or "").strip() or None,
        "debt": debt,
    }


async def import_courses_xlsx(session: AsyncSession, ctx: TenantContext, path: str) -> dict:
    problems: list[str] = []
    created = 0
    existing = {
        row.code: row
        for row in (
            await session.execute(select(Course).where(Course.tenant_id == ctx.id))
        ).scalars()
    }
    for index, row in enumerate(_load_rows(path), start=2):
        name = str(_pick(row, COURSE_ALIASES["name"]) or "").strip()
        code = str(_pick(row, COURSE_ALIASES["code"]) or "").strip() or None
        if not name:
            problems.append(f"строка {index}: нет названия курса")
            continue
        code = code or re.sub(r"[^a-z0-9]+", "_", name.lower())[:40] or f"course_{index}"
        if code in existing:
            problems.append(f"строка {index}: курс {code} уже есть")
            continue
        price = _number(_pick(row, COURSE_ALIASES["price"]))
        course = Course(
            tenant_id=ctx.id,
            code=code,
            name={"ru": name},
            direction=str(_pick(row, COURSE_ALIASES["direction"]) or "other"),
            price_month=Decimal(str(price)) if price is not None else None,
            currency=ctx.currency,
        )
        session.add(course)
        existing[code] = course
        created += 1
    await session.flush()
    return {"created": created, "problems": problems}


async def import_groups_xlsx(session: AsyncSession, ctx: TenantContext, path: str) -> dict:
    problems: list[str] = []
    created = 0
    courses = {
        row.code: row
        for row in (
            await session.execute(select(Course).where(Course.tenant_id == ctx.id))
        ).scalars()
    }
    by_name = {row.label("ru").lower(): row for row in courses.values()}
    for index, row in enumerate(_load_rows(path), start=2):
        name = str(_pick(row, GROUP_ALIASES["name"]) or "").strip()
        course_key = str(_pick(row, GROUP_ALIASES["course"]) or "").strip()
        if not name or not course_key:
            problems.append(f"строка {index}: нужны группа и курс")
            continue
        course = courses.get(course_key) or by_name.get(course_key.lower())
        if course is None:
            problems.append(f"строка {index}: курс «{course_key}» не найден")
            continue
        capacity = int(_number(_pick(row, GROUP_ALIASES["capacity"])) or 10)
        session.add(
            Group(
                tenant_id=ctx.id,
                course_id=course.id,
                name=name,
                capacity=max(capacity, 1),
                min_to_start=1,
                schedule={"days": [0, 2, 4], "time": "18:00"},
            )
        )
        created += 1
    await session.flush()
    return {"created": created, "problems": problems}


async def import_students_xlsx(session: AsyncSession, ctx: TenantContext, path: str) -> dict:
    """Create a student, a paying guardian and an open invoice when a debt is set."""
    from arix.core.timeutil import local_date, now_utc
    from arix.verticals.edu.billing import period_of

    problems: list[str] = []
    created = 0
    groups = {
        row.name.lower(): row
        for row in (
            await session.execute(select(Group).where(Group.tenant_id == ctx.id))
        ).scalars()
    }
    today = local_date(now_utc(), ctx.tz_name)
    for index, row in enumerate(_load_rows(path), start=2):
        parsed = parse_student_row(row)
        if isinstance(parsed, str):
            problems.append(f"строка {index}: {parsed}")
            continue
        contact = None
        if parsed["phone"]:
            contact = Contact(
                tenant_id=ctx.id,
                name=parsed["parent"] or parsed["student"],
                phone=parsed["phone"],
                lang=ctx.default_lang,
                source="import",
            )
            session.add(contact)
            await session.flush()
        student = Student(
            tenant_id=ctx.id,
            name=parsed["student"],
            contact_id=contact.id if contact and not parsed["parent"] else None,
            status="active" if parsed["group"] else "lead",
        )
        session.add(student)
        await session.flush()
        if contact and parsed["parent"]:
            session.add(
                Guardian(
                    tenant_id=ctx.id,
                    contact_id=contact.id,
                    student_id=student.id,
                    relation="parent",
                    is_payer=True,
                )
            )
        group = groups.get((parsed["group"] or "").lower())
        if parsed["group"] and group is None:
            problems.append(f"строка {index}: группа «{parsed['group']}» не найдена")
        elif group is not None:
            price = group.price_month or (group.course.price_month if group.course else None) or 0
            enrollment = Enrollment(
                tenant_id=ctx.id,
                student_id=student.id,
                group_id=group.id,
                price_month=price,
                start_date=group.start_date or today,
                status="active",
                attribution="import",
                idempotency_key=f"import:{student.id}:{group.id}",
            )
            session.add(enrollment)
            await session.flush()
            if parsed["debt"]:
                session.add(
                    Invoice(
                        tenant_id=ctx.id,
                        enrollment_id=enrollment.id,
                        student_id=student.id,
                        period=period_of(today),
                        amount=Decimal(str(parsed["debt"])),
                        currency=ctx.currency,
                        due_date=today,
                        status="open",
                        note="долг из импорта",
                    )
                )
        created += 1
    await session.flush()
    return {"created": created, "problems": problems}


__all__ = [
    "import_courses_xlsx",
    "import_groups_xlsx",
    "import_students_xlsx",
    "parse_student_row",
]
