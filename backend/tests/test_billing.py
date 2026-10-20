"""Invoice arithmetic does not need a database."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from arix.verticals.edu.billing import (
    build_ladder,
    due_date_for,
    ladder_runs,
    prorate,
)


def test_prorate_full_month():
    amount, note = prorate(1_000_000, 0, "2026-03", start_date=date(2026, 3, 1))
    assert amount == Decimal("1000000.00")
    assert note is None


def test_prorate_from_the_20th():
    amount, note = prorate(310_000, 0, "2026-03", start_date=date(2026, 3, 20))
    # March has 31 days, active days = 12.
    assert amount == Decimal("120000.00")
    assert note and "12" in note


def test_prorate_with_discount():
    amount, note = prorate(100_000, 10, "2026-04", start_date=date(2026, 4, 1))
    assert amount == Decimal("90000.00")
    assert note is None


def test_prorate_course_ends_mid_month():
    amount, note = prorate(
        300_000, 0, "2026-02", start_date=date(2026, 2, 1), end_date=date(2026, 2, 14)
    )
    # 2026 is not a leap year: 14 of 28 days.
    assert amount == Decimal("150000.00")
    assert note and "14" in note


def test_prorate_outside_the_period_is_zero():
    amount, note = prorate(
        100_000, 0, "2026-05", start_date=date(2026, 6, 1), end_date=date(2026, 6, 30)
    )
    assert amount == Decimal("0.00")
    assert note == "вне периода обучения"


def test_prorate_course_mode_bills_once():
    first, note = prorate(
        2_000_000, 0, "2026-09", start_date=date(2026, 9, 5), mode="course"
    )
    later, later_note = prorate(
        2_000_000, 0, "2026-10", start_date=date(2026, 9, 5), mode="course"
    )
    assert first == Decimal("2000000.00")
    assert "курс" in (note or "")
    assert later == Decimal("0.00")
    assert later_note == "курс оплачен ранее"


def test_due_date_by_start_and_fixed_day_and_february():
    assert due_date_for("2026-03", 0, start_date=date(2026, 3, 20)) == date(2026, 3, 20)
    assert due_date_for("2026-04", 5, start_date=date(2026, 3, 20)) == date(2026, 4, 5)
    # Day 31 clamps to the last day of February.
    assert due_date_for("2026-02", 31, start_date=date(2026, 1, 31)) == date(2026, 2, 28)


def test_ladder_last_positive_step_is_a_handoff():
    steps = build_ladder([-3, 0, 3, 7, 10])
    assert [step.offset_days for step in steps] == [-3, 0, 3, 7, 10]
    assert steps[-1].is_handoff
    assert not steps[0].is_handoff
    runs = ladder_runs(date(2026, 5, 10), [-3, 0, 10], timezone="Asia/Tashkent")
    assert len(runs) == 3
    assert runs[0][1].day == 7  # 10 May minus 3 days, in UTC still the 7th morning
