"""Placement-test scoring uses the centre's own bands."""
from __future__ import annotations

from arix.verticals.edu.leveltest import level_for, score_answers
from arix.verticals.edu.models import LevelTest


def test_level_for_uses_bands():
    scoring = {"bands": [{"min": 0, "max": 3, "level": "A1"}, {"min": 4, "max": 6, "level": "A2"}]}
    assert level_for(2, scoring) == "A1"
    assert level_for(5, scoring) == "A2"
    assert level_for(9, scoring) is None


def test_score_counts_only_matching_answers():
    test = LevelTest(
        tenant_id="00000000-0000-0000-0000-000000000001",
        code="t",
        title="t",
        questions={"items": [
            {"q": "a", "options": ["1", "2"], "answer": 0},
            {"q": "b", "options": ["1", "2"], "answer": 1},
        ]},
        scoring={"bands": []},
    )
    assert score_answers(test, {"0": 0, "1": 1}) == 2
    assert score_answers(test, {"0": 1, "1": 1}) == 1
