"""Education-centre data model (ТЗ B3).

The unit of enrolment is a *seat in a group*, not a time slot, so capacity —
not an exclusion constraint — is what must never be exceeded.  Money is
recurring rather than per-visit, which is why invoices and the debt ladder are
first-class here.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arix.db.base import GUID, Base, JSONColumn, PKMixin, TenantMixin, TimestampMixin

DIRECTIONS = ("lang", "exam", "it", "kids", "prof", "other")
GROUP_STATUSES = ("recruiting", "running", "paused", "closed")
STUDENT_STATUSES = ("lead", "trial", "active", "paused", "left")
TRIAL_STATUSES = ("booked", "confirmed", "attended", "no_show", "cancelled")
ENROLLMENT_STATUSES = ("reserved", "active", "paused", "finished", "cancelled")
INVOICE_STATUSES = ("open", "paid", "overdue", "written_off", "cancelled")
LESSON_STATUSES = ("planned", "done", "cancelled", "moved")
ATTENDANCE_STATUSES = ("present", "absent", "late", "excused")


class Course(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "courses"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="lang")
    age_from: Mapped[int | None] = mapped_column(SmallInteger)
    age_to: Mapped[int | None] = mapped_column(SmallInteger)
    level_from: Mapped[str | None] = mapped_column(String(16))
    level_to: Mapped[str | None] = mapped_column(String(16))
    price_month: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="UZS")
    lessons_per_week: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    lesson_min: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    duration_months: Mapped[int | None] = mapped_column(SmallInteger)
    has_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    has_level_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Free-text goals that should route to this course (``match_course``).
    goal_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_courses_code"),
        CheckConstraint(f"direction IN {DIRECTIONS}", name="direction_valid"),
        CheckConstraint("lesson_min > 0", name="lesson_min_positive"),
        CheckConstraint(
            "age_to IS NULL OR age_from IS NULL OR age_to >= age_from", name="age_range_sane"
        ),
    )

    def label(self, lang: str = "ru") -> str:
        return self.name.get(lang) or self.name.get("ru") or self.code


class Teacher(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "teachers"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    subjects: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    langs: Mapped[list[str]] = mapped_column(ARRAY(String(8)), nullable=False, default=list)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    bio_short: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Group(PKMixin, TimestampMixin, TenantMixin, Base):
    """A study group. ``capacity`` is enforced in ``enrollment.py``."""

    __tablename__ = "groups"

    course_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    teacher_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("teachers.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    level: Mapped[str | None] = mapped_column(String(32))
    #: ``{"days": [0, 2, 4], "time": "18:00"}`` — local to the tenant.
    schedule: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    room: Mapped[str | None] = mapped_column(String(64))
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=10)
    min_to_start: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=4)
    start_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="recruiting")
    price_month: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    course: Mapped[Course] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(f"status IN {GROUP_STATUSES}", name="status_valid"),
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("min_to_start >= 0 AND min_to_start <= capacity", name="min_to_start_sane"),
        Index("ix_groups_tenant_status", "tenant_id", "status"),
    )


class Student(PKMixin, TimestampMixin, TenantMixin, Base):
    """A learner. Adults have their own ``contact_id``; children do not —
    their data is collected through a guardian (ТЗ B10)."""

    __tablename__ = "students"

    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    birth_year: Mapped[int | None] = mapped_column(SmallInteger)
    level: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="lead")
    left_reason: Mapped[str | None] = mapped_column(String(255))
    churn_risk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"status IN {STUDENT_STATUSES}", name="status_valid"),
        CheckConstraint(
            "birth_year IS NULL OR (birth_year > 1900 AND birth_year < 2100)",
            name="birth_year_sane",
        ),
        Index("ix_students_tenant_status", "tenant_id", "status"),
    )

    @property
    def is_minor_guess(self) -> bool:
        """Used only to decide whether to talk to a guardian, never for policy."""
        if self.birth_year is None:
            return False
        from datetime import UTC
        from datetime import datetime as _dt

        return (_dt.now(UTC).year - self.birth_year) < 18


class Guardian(PKMixin, TimestampMixin, TenantMixin, Base):
    """Links a paying adult to a student. Balances go only to ``is_payer``."""

    __tablename__ = "guardians"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation: Mapped[str | None] = mapped_column(String(32))
    is_payer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("contact_id", "student_id", name="uq_guardian_pair"),
    )


class TrialLesson(PKMixin, TimestampMixin, TenantMixin, Base):
    """A trial slot: either inside a running group or a standalone session."""

    __tablename__ = "trial_lessons"

    course_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    teacher_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    room: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint(
            "course_id IS NOT NULL OR group_id IS NOT NULL", name="trial_target_required"
        ),
    )


class TrialBooking(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "trial_bookings"

    trial_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("trial_lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="booked")
    #: The teacher's one-tap verdict after the trial (1–5).
    feedback_score: Mapped[int | None] = mapped_column(SmallInteger)
    teacher_note: Mapped[str | None] = mapped_column(Text)
    recommended_group_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    attribution: Mapped[str] = mapped_column(String(32), nullable=False, default="bot")
    idempotency_key: Mapped[str | None] = mapped_column(String(190))

    __table_args__ = (
        CheckConstraint(f"status IN {TRIAL_STATUSES}", name="status_valid"),
        CheckConstraint(
            "feedback_score IS NULL OR feedback_score BETWEEN 1 AND 5", name="score_range"
        ),
        # One student cannot take the same trial slot twice.
        UniqueConstraint("trial_id", "student_id", name="uq_trial_booking_student"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_trial_bookings_idempotency"),
    )


class Enrollment(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "enrollments"

    student_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_month: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    #: Day of month the monthly invoice is issued; 0 means "по дате старта".
    billing_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    attribution: Mapped[str] = mapped_column(String(32), nullable=False, default="bot")
    idempotency_key: Mapped[str | None] = mapped_column(String(190))

    __table_args__ = (
        CheckConstraint(f"status IN {ENROLLMENT_STATUSES}", name="status_valid"),
        CheckConstraint("discount_pct BETWEEN 0 AND 100", name="discount_range"),
        CheckConstraint("billing_day BETWEEN 0 AND 28", name="billing_day_range"),
        # A student holds at most one seat in a given group.
        UniqueConstraint("student_id", "group_id", name="uq_enrollment_seat"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_enrollments_idempotency"),
        Index("ix_enrollments_tenant_status", "tenant_id", "status"),
    )

    @property
    def monthly_due(self) -> Decimal:
        return (self.price_month * (100 - self.discount_pct) / 100).quantize(Decimal("0.01"))


class Invoice(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "invoices"

    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: Billing period as ``YYYY-MM``.
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="UZS")
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    #: Marks the invoice as prorated for a mid-month start.
    note: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint(f"status IN {INVOICE_STATUSES}", name="status_valid"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        # One invoice per enrolment per period — the generator is idempotent.
        UniqueConstraint("enrollment_id", "period", name="uq_invoice_period"),
        Index("ix_invoices_tenant_status_due", "tenant_id", "status", "due_date"),
    )

    @property
    def is_debt(self) -> bool:
        return self.status in ("open", "overdue")


class Lesson(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "lessons"

    group_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    topic: Mapped[str | None] = mapped_column(String(255))
    homework: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    attendance_marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"status IN {LESSON_STATUSES}", name="status_valid"),
        UniqueConstraint("group_id", "starts_at", name="uq_lesson_slot"),
    )


class Attendance(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "attendance"

    lesson_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="present")
    #: Set once the parent has been told about an absence, so we never tell twice.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint(f"status IN {ATTENDANCE_STATUSES}", name="status_valid"),
        UniqueConstraint("lesson_id", "student_id", name="uq_attendance_mark"),
    )


class LevelTest(PKMixin, TimestampMixin, TenantMixin, Base):
    """A button-driven placement test (ТЗ B5 У2)."""

    __tablename__ = "level_tests"

    course_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    #: ``[{"q": "...", "options": ["a", "b"], "answer": 0}]``
    questions: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: ``[{"min": 0, "max": 4, "level": "A1"}]``
    scoring: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    lang: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_level_tests_code"),)


class TestResult(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "test_results"

    student_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    test_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    level: Mapped[str | None] = mapped_column(String(32))
    answers: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Referral(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "referrals"

    referrer_student_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    new_student_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    reward_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_referral_code"),
        CheckConstraint(
            "reward_status IN ('pending', 'earned', 'granted', 'void')", name="reward_valid"
        ),
    )


class Waitlist(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "waitlist"

    course_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    contact_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    preferred_time: Mapped[time | None] = mapped_column(Time)
    note: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'offered', 'enrolled', 'closed')", name="status_valid"
        ),
    )


__all__ = [
    "ATTENDANCE_STATUSES",
    "DIRECTIONS",
    "ENROLLMENT_STATUSES",
    "GROUP_STATUSES",
    "INVOICE_STATUSES",
    "LESSON_STATUSES",
    "STUDENT_STATUSES",
    "TRIAL_STATUSES",
    "Attendance",
    "Course",
    "Enrollment",
    "Group",
    "Guardian",
    "Invoice",
    "Lesson",
    "LevelTest",
    "Referral",
    "Student",
    "Teacher",
    "TestResult",
    "TrialBooking",
    "TrialLesson",
    "Waitlist",
]
