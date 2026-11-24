"""The judge's result clip keeps the END of a long report (2026-11-24).

A plain head slice hid the "dependency-ordered implementation plan" that many
step criteria ask for; the clip is head+tail with a visible omission marker.
"""

from __future__ import annotations

import pytest

from agent.tools.taskflow.step_judge import _clip_for_judge
from config.features import STEP_JUDGE

pytestmark = [pytest.mark.unit]

_BUDGET = int(STEP_JUDGE["max_result_chars"])


def test_a_short_result_is_unchanged() -> None:
    assert _clip_for_judge("short report") == "short report"


def test_a_long_result_keeps_its_head_and_tail() -> None:
    text = "HEAD-MARKER\n" + ("filler line\n" * 1000) + "FINAL IMPLEMENTATION PLAN"

    clipped = _clip_for_judge(text)

    assert len(clipped) == _BUDGET
    assert clipped.startswith("HEAD-MARKER")
    assert clipped.endswith("FINAL IMPLEMENTATION PLAN")
    assert "middle omitted" in clipped


def test_the_budget_is_configurable() -> None:
    text = "A" * 300 + "TAIL"

    clipped = _clip_for_judge(text, limit=200)

    assert len(clipped) == 200
    assert clipped.endswith("TAIL")
