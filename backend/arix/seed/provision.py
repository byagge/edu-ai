"""Tenant provisioning: create a centre and seed it from a template.

This is what makes "настройка центра ≤ 4 часов" true, and the demo generator
reach 60 seconds.  Idempotent per slug: re-running updates config and tops up
missing rows instead of duplicating them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.config import get_settings
from arix.core.context import TenantContext
from arix.core.events import audit
from arix.core.timeutil import local_date, now_utc
from arix.db.models_core import Channel, KBItem, Sequence, Staff, Tenant, TenantConfig
from arix.logging_setup import get_logger
from arix.verticals.edu.models import Course, Group, LevelTest, Teacher

from .template_packs import Template, get_template

log = get_logger("arix.provision")


@dataclass
class ProvisionResult:
    tenant: Tenant
    created: bool
    counts: dict[str, int]


async def create_tenant(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    vertical: str = "edu",
    country: str = "UZ",
    city: str | None = None,
    timezone: str = "Asia/Tashkent",
    languages: list[str] | None = None,
    template_code: str | None = "edu_language",
    status: str = "pilot",
    plan: str = "pilot",
    demo_ttl_days: int | None = None,
    source_ref: dict | None = None,
) -> ProvisionResult:
    existing = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    created = existing is None
    s = get_settings()

    if existing is None:
        tenant = Tenant(
            slug=slug,
            name=name,
            vertical=vertical,
            country=country.upper(),
            city=city,
            timezone=timezone,
            languages=languages or ["ru"],
            plan=plan,
            status=status,
            data_region=s.data_region,
            template=template_code,
            source_ref=source_ref,
            demo_expires_at=(
                now_utc() + timedelta(days=demo_ttl_days or s.demo_ttl_days)
                if status == "demo"
                else None
            ),
        )
        session.add(tenant)
        await session.flush()
    else:
        tenant = existing
        tenant.name = name
        tenant.city = city or tenant.city
        tenant.timezone = timezone or tenant.timezone
        if languages:
            tenant.languages = languages
        tenant.template = template_code or tenant.template
        await session.flush()

    template = get_template(template_code)
    counts: dict[str, int] = {}
    if template is not None:
        counts = await apply_template(session, tenant, template, languages=languages)

    await audit(
        session, tenant.id, actor="system", action="tenant_provisioned",
        entity="tenant", entity_id=str(tenant.id),
        data={"template": template_code, "created": created, "counts": counts},
    )
    log.info("tenant_provisioned", slug=slug, created=created, counts=counts)
    return ProvisionResult(tenant=tenant, created=created, counts=counts)


async def apply_template(
    session: AsyncSession,
    tenant: Tenant,
    template: Template,
    *,
    languages: list[str] | None = None,
    overrides: dict | None = None,
) -> dict[str, int]:
    counts = {
        "courses": 0, "teachers": 0, "groups": 0, "level_tests": 0,
        "kb": 0, "sequences": 0,
    }

    config = dict(template.config)
    if overrides:
        config = _deep_merge(config, overrides)
    await publish_config(session, tenant.id, config, note=f"template:{template.code}")
    currency = config.get("currency", "UZS")

    existing_courses = {
        row.code: row
        for row in (
            await session.execute(select(Course).where(Course.tenant_id == tenant.id))
        ).scalars()
    }
    course_by_code: dict[str, Course] = dict(existing_courses)
    for item in template.courses:
        if item["code"] in existing_courses:
            continue
        course = Course(
            tenant_id=tenant.id,
            code=item["code"],
            name=item.get("name") or {"ru": item["code"]},
            direction=item.get("direction", "lang"),
            age_from=item.get("age_from"),
            age_to=item.get("age_to"),
            level_from=item.get("level_from"),
            level_to=item.get("level_to"),
            price_month=Decimal(str(item["price_month"])) if item.get("price_month") else None,
            currency=currency,
            lessons_per_week=int(item.get("lessons_per_week", 2)),
            lesson_min=int(item.get("lesson_min", 90)),
            duration_months=item.get("duration_months"),
            has_trial=bool(item.get("has_trial", True)),
            has_level_test=bool(item.get("has_level_test", False)),
            goal_keywords=item.get("goal_keywords") or [],
            sort=int(item.get("sort", 100)),
        )
        session.add(course)
        course_by_code[item["code"]] = course
        counts["courses"] += 1
    await session.flush()

    existing_teachers = {
        row.name: row
        for row in (
            await session.execute(select(Teacher).where(Teacher.tenant_id == tenant.id))
        ).scalars()
    }
    teacher_by_name: dict[str, Teacher] = dict(existing_teachers)
    for index, item in enumerate(template.teachers):
        if item["name"] in existing_teachers:
            continue
        teacher = Teacher(
            tenant_id=tenant.id,
            name=item["name"],
            subjects=item.get("subjects") or [],
            langs=item.get("langs") or languages or ["ru"],
            bio_short=item.get("bio_short"),
            sort=index,
        )
        session.add(teacher)
        teacher_by_name[item["name"]] = teacher
        counts["teachers"] += 1
    await session.flush()

    existing_groups = {
        row.name
        for row in (
            await session.execute(select(Group.name).where(Group.tenant_id == tenant.id))
        ).scalars()
    }
    today = local_date(now_utc(), tenant.timezone)
    for item in template.groups:
        if item["name"] in existing_groups:
            continue
        course = course_by_code.get(item.get("course"))
        if course is None:
            continue
        teacher = teacher_by_name.get(item.get("teacher", ""))
        starts_in = int(item.get("starts_in_days", 7))
        session.add(
            Group(
                tenant_id=tenant.id,
                course_id=course.id,
                teacher_id=teacher.id if teacher else None,
                name=item["name"],
                level=item.get("level"),
                schedule=item.get("schedule") or {},
                room=item.get("room"),
                capacity=int(item.get("capacity", 10)),
                min_to_start=int(item.get("min_to_start", 4)),
                start_date=today + timedelta(days=starts_in),
                status=item.get("status", "recruiting"),
                price_month=(
                    Decimal(str(item["price_month"])) if item.get("price_month") else None
                ),
            )
        )
        counts["groups"] += 1
    await session.flush()

    if template.level_test:
        test = template.level_test
        exists = (
            await session.execute(
                select(LevelTest).where(
                    LevelTest.tenant_id == tenant.id, LevelTest.code == test.get("code", "default")
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            course = course_by_code.get(test.get("course"))
            session.add(
                LevelTest(
                    tenant_id=tenant.id,
                    course_id=course.id if course else None,
                    code=test.get("code", "default"),
                    title=test.get("title", "Тест уровня"),
                    questions={"items": test.get("questions") or []},
                    scoring={"bands": test.get("bands") or []},
                    lang=test.get("lang", "ru"),
                )
            )
            counts["level_tests"] += 1
    await session.flush()

    existing_kb = {
        row.question
        for row in (
            await session.execute(select(KBItem.question).where(KBItem.tenant_id == tenant.id))
        ).scalars()
    }
    for item in template.kb:
        if item["question"] in existing_kb:
            continue
        session.add(
            KBItem(
                tenant_id=tenant.id,
                type=item.get("type", "faq"),
                question=item["question"],
                answer=item["answer"],
                lang=item.get("lang", "ru"),
                tags=item.get("tags") or [],
                verbatim=bool(item.get("verbatim")),
            )
        )
        counts["kb"] += 1

    existing_sequences = {
        row.code
        for row in (
            await session.execute(select(Sequence.code).where(Sequence.tenant_id == tenant.id))
        ).scalars()
    }
    for code, body in (template.sequences or {}).items():
        if code in existing_sequences:
            continue
        session.add(Sequence(tenant_id=tenant.id, code=code, steps=body))
        counts["sequences"] += 1

    await session.flush()
    return counts


async def publish_config(
    session: AsyncSession, tenant_id: uuid.UUID, data: dict, *, note: str | None = None
) -> TenantConfig:
    """Store a new config version and publish it.

    Versions are append-only: rolling back is choosing an older version, never
    editing history.
    """
    from arix.core.tenant_config import TenantSettings

    TenantSettings.from_data(data)  # validate before publishing

    current = (
        await session.execute(
            select(TenantConfig)
            .where(TenantConfig.tenant_id == tenant_id)
            .order_by(TenantConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    merged = _deep_merge(current.data if current else {}, data)
    row = TenantConfig(
        tenant_id=tenant_id,
        version=(current.version + 1) if current else 1,
        data=merged,
        published_at=now_utc(),
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def ensure_channel(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channel_type: str,
    *,
    credentials: dict | None = None,
    settings: dict | None = None,
    status: str = "pending",
    external_id: str | None = None,
) -> Channel:
    from arix.core.crypto import encrypt_json

    channel = (
        await session.execute(
            select(Channel).where(Channel.tenant_id == tenant_id, Channel.type == channel_type)
        )
    ).scalars().first()
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type=channel_type, settings={})
        session.add(channel)
    if credentials:
        channel.credentials_enc = encrypt_json(credentials)
    if settings:
        channel.settings = {**(channel.settings or {}), **settings}
    if external_id:
        channel.external_id = external_id
    channel.status = status
    await session.flush()
    return channel


async def ensure_staff(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    role: str,
    name: str,
    tg_user_id: int | None = None,
    email: str | None = None,
    password: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> Staff:
    stmt = select(Staff).where(Staff.tenant_id == tenant_id, Staff.role == role)
    if tg_user_id:
        stmt = stmt.where(Staff.tg_user_id == tg_user_id)
    elif email:
        stmt = stmt.where(Staff.email == email)
    member = (await session.execute(stmt)).scalars().first()
    if member is None:
        member = Staff(tenant_id=tenant_id, role=role, name=name)
        session.add(member)
    member.name = name
    if tg_user_id:
        member.tg_user_id = tg_user_id
    if email:
        member.email = email
    if entity_id:
        member.entity_id = entity_id
    if password:
        from arix.core.passwords import hash_password

        member.password_hash = hash_password(password)
    member.active = True
    await session.flush()
    return member


async def seed_demo_data(
    session: AsyncSession, ctx: TenantContext, *, students_per_group: int = 4
) -> dict[str, int]:
    """Fill a demo centre with students, enrolments, invoices and lessons.

    Without this the panel and the owner report would be empty, and a demo of
    "долги и наполненность групп" would show nothing.
    """
    from arix.db.models_core import Contact, Conversation
    from arix.verticals.edu import attendance as att
    from arix.verticals.edu import billing as bl
    from arix.verticals.edu import enrollment as en
    from arix.verticals.edu import trials as tr

    groups = list(
        (await session.execute(select(Group).where(Group.tenant_id == ctx.id))).scalars()
    )
    if not groups:
        return {"students": 0}

    names = [
        "Азиз Р.", "Мадина К.", "Дилшод А.", "Камила Н.", "Санжар Т.",
        "Нигора Х.", "Тимур Б.", "Алия С.",
    ]
    today = local_date(now_utc(), ctx.tz_name)
    created = {"students": 0, "enrollments": 0, "invoices": 0, "lessons": 0, "trials": 0}
    index = 0

    for group in groups[:3]:
        group.status = "running"
        for _ in range(students_per_group):
            if index >= len(names):
                break
            name = names[index]
            contact = Contact(
                tenant_id=ctx.id,
                phone=f"+99890000{index:04d}",
                name=f"Родитель {name.split()[0]}",
                lang=ctx.default_lang,
                source="import",
                consent_pd_at=now_utc(),
                consent_marketing_at=now_utc(),
            )
            session.add(contact)
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=ctx.id, contact_id=contact.id, channel_type="tg",
                    peer_id=f"demo-{contact.id}", state="active",
                )
            )
            student = await en.ensure_student(
                session, ctx, name=name, contact=contact, birth_year=2012 + (index % 6)
            )
            created["students"] += 1
            try:
                # Stagger start dates so prorated invoices appear in the demo.
                enrollment, invoice = await en.enroll(
                    session, ctx,
                    student_id=student.id, group_id=group.id, attribution="import",
                    start_date=today - timedelta(days=10 + index * 3),
                    idempotency_key=f"demo:{student.id}:{group.id}",
                )
                created["enrollments"] += 1
                if invoice is not None:
                    created["invoices"] += 1
                    # Leave one debt per group so the owner report is not empty.
                    if index % students_per_group != 0:
                        await bl.mark_invoice_paid(session, ctx, invoice.id)
                    else:
                        invoice.due_date = today - timedelta(days=12)
                        invoice.status = "overdue"
                await en.activate_enrollment(session, ctx, enrollment.id)
            except (en.SeatsFull, en.AlreadyEnrolled):
                pass
            index += 1

        created["lessons"] += await en.generate_lessons(
            session, ctx, group, weeks=2, from_date=today - timedelta(days=7)
        )

    # One past lesson with an absence, so churn risk has something to show.
    past = list(
        (
            await session.execute(
                select(att.Lesson)
                .where(
                    att.Lesson.tenant_id == ctx.id,
                    att.Lesson.starts_at < now_utc(),
                    att.Lesson.status == "planned",
                )
                .order_by(att.Lesson.starts_at)
                .limit(2)
            )
        ).scalars()
    )
    for lesson in past:
        students = await en.group_students(session, ctx.id, lesson.group_id)
        if students:
            await att.mark_attendance(session, ctx, lesson.id, [students[0].id])

    # A couple of trial slots so the first demo dialogue can convert.
    courses = list(
        (await session.execute(select(Course).where(Course.tenant_id == ctx.id))).scalars()
    )
    for course in courses[:2]:
        created["trials"] += await tr.ensure_trial_slots(session, ctx, course.id)

    await session.flush()
    return created


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


__all__ = [
    "ProvisionResult",
    "apply_template",
    "create_tenant",
    "ensure_channel",
    "ensure_staff",
    "publish_config",
    "seed_demo_data",
]
