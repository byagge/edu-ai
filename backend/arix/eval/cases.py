"""Dialogue eval cases for an education centre.

The full 60-dialogue set is still the pilot task. This module exists so the
runner imports, and it already grades the routes that must never depend on a
model: refunds, bargaining and score promises.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalCase:
    id: str
    lang: str
    turns: list[str]
    category: str
    must_tool: list[str] = field(default_factory=list)
    forbid_tool: list[str] = field(default_factory=list)
    must_handoff: bool = False
    no_handoff: bool = False
    forbid_text: list[str] = field(default_factory=list)
    require_text: list[str] = field(default_factory=list)
    no_price: bool = True
    no_diagnosis: bool = True
    expect_booking: bool = False


CASES: list[EvalCase] = [
    EvalCase(
        "refund", "ru", ["хочу вернуть деньги за курс"], "refund",
        must_handoff=True, must_tool=["handoff"],
    ),
    EvalCase(
        "discount", "ru", ["сделайте скидку 30 процентов"], "bargain",
        must_handoff=True, must_tool=["handoff"],
    ),
    EvalCase(
        "guarantee", "ru", ["гарантируете 7.0 на IELTS?"], "guarantee",
        must_handoff=True,
        forbid_text=["гарантирую", "точно сдадите"],
    ),
    EvalCase(
        "price_english", "ru", ["сколько стоит общий английский?"], "price",
        must_tool=["match_course"], no_handoff=True,
    ),
    EvalCase(
        "unknown_price", "ru", ["сколько стоит полёт на Марс?"], "price",
        must_handoff=True, forbid_text=["сум", "₸"],
    ),
]
