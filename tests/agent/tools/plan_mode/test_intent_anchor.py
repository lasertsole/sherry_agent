"""Unit tests for the approved-plan INTENT anchor.

Covers the three contracts that keep execution from drifting:
1. the plan's Goal / Scope / accept: text is parsed into a compact record;
2. the record is dual-written to the state registers and rendered into the
   per-turn todo reminder;
3. a plan file edited after approval is detected as drift and blocks verify.
"""

from __future__ import annotations

import hashlib

import pytest

import agent.tools.plan_mode.intent_anchor as anchor_mod
from runtime import StateKey

pytestmark = [pytest.mark.unit]

PLAN = """## Goal
Make judge outage fail closed instead of silently passing.

## Scope
- Must-have: StepJudge returns UNKNOWN on provider error
- Must-have: taskflow_resume blocks the step
- Must-NOT-have: no fail-open by default

## Steps
1. Add StepVerdict.UNKNOWN — depends on: none — accept: outage yields UNKNOWN
2. Wire the resume gate — depends on: 1 — accept: blocked status asserted

## Verification
pytest tests/agent/tools/taskflow -q

## Risk
Old snapshots may rely on fail-open; keep an opt-in escape.
"""


class _FakeRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.data[(session_id, str(key))] = value
        return True

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, str(key)), default)

    def delete_state(self, session_id: str, key: str) -> bool:
        return self.data.pop((session_id, str(key)), None) is not None


@pytest.fixture()
def registers(monkeypatch):
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(anchor_mod, "state_register_mem", mem)
    monkeypatch.setattr(anchor_mod, "state_register_db", db)
    return {"mem": mem, "db": db}


class TestParse:
    def test_goal_scope_and_acceptance_are_extracted(self):
        revision = hashlib.sha256(PLAN.encode()).hexdigest()[:16]
        anchor = anchor_mod.parse_intent_anchor(PLAN, revision=revision, source_ref="/tmp/plan.md")

        assert anchor_mod.revision_of(PLAN) == revision
        assert anchor["goal"] == "Make judge outage fail closed instead of silently passing."
        assert anchor["must_have"] == [
            "StepJudge returns UNKNOWN on provider error",
            "taskflow_resume blocks the step",
        ]
        assert anchor["must_not_have"] == ["no fail-open by default"]
        assert any("outage yields UNKNOWN" in item for item in anchor["acceptance"])
        assert any("overall: pytest" in item for item in anchor["acceptance"])
        assert anchor["revision"] == revision
        assert anchor["source_ref"] == "/tmp/plan.md"

    def test_malformed_plan_still_yields_an_anchor(self):
        anchor = anchor_mod.parse_intent_anchor("just prose", revision="abc")

        assert anchor["revision"] == "abc"
        assert anchor["goal"] == ""
        assert anchor["must_have"] == []
        assert anchor["acceptance"] == []


class TestFormat:
    def test_render_contains_all_anchor_fields(self):
        anchor = anchor_mod.parse_intent_anchor(PLAN, revision="r1")
        text = anchor_mod.format_intent_anchor(anchor)

        assert "Intent Anchor" in text
        assert "Make judge outage fail closed" in text
        assert "Must-have: StepJudge returns UNKNOWN" in text
        assert "Must-NOT-have: no fail-open by default" in text
        assert "Accept:" in text
        assert "enter_plan_mode(replan=True)" in text

    def test_empty_anchor_renders_nothing(self):
        assert anchor_mod.format_intent_anchor({}) == ""
        assert anchor_mod.format_intent_anchor(None) == ""  # type: ignore[arg-type]


class TestRegisterRoundTrip:
    def test_write_is_dual_and_clear_removes_both(self, registers):
        anchor = anchor_mod.parse_intent_anchor(PLAN, revision="r1", source_ref="/tmp/p.md")
        anchor_mod.write_intent_anchor("s1", anchor)

        assert registers["mem"].get_state("s1", StateKey.PLAN_INTENT_ANCHOR) == anchor
        assert registers["db"].get_state("s1", StateKey.PLAN_INTENT_ANCHOR) == anchor
        assert anchor_mod.read_intent_anchor("s1") == anchor

        anchor_mod.clear_intent_anchor("s1")
        assert anchor_mod.read_intent_anchor("s1") is None

    def test_db_fallback_when_memory_tier_is_cold(self, registers):
        anchor = {"goal": "g", "revision": "r1"}
        registers["db"].set_state("s1", StateKey.PLAN_INTENT_ANCHOR, anchor)

        assert anchor_mod.read_intent_anchor("s1") == anchor


class TestDrift:
    def test_unchanged_plan_is_not_drift(self, registers, tmp_path):
        path = tmp_path / "plan.md"
        plan_text = PLAN.strip()  # exit_plan_mode strips before hashing/persisting
        path.write_text(plan_text, encoding="utf-8")
        anchor = anchor_mod.parse_intent_anchor(
            plan_text, revision=anchor_mod.revision_of(plan_text), source_ref=str(path)
        )
        anchor_mod.write_intent_anchor("s1", anchor)

        assert anchor_mod.plan_drifted("s1") is False

    def test_edited_plan_is_drift(self, registers, tmp_path):
        path = tmp_path / "plan.md"
        plan_text = PLAN.strip()
        path.write_text(plan_text, encoding="utf-8")
        anchor = anchor_mod.parse_intent_anchor(
            plan_text, revision=anchor_mod.revision_of(plan_text), source_ref=str(path)
        )
        anchor_mod.write_intent_anchor("s1", anchor)

        path.write_text(plan_text + "\n- Must-have: something else entirely\n", encoding="utf-8")

        assert anchor_mod.plan_drifted("s1") is True

    def test_legacy_session_without_anchor_fails_open(self, registers):
        assert anchor_mod.plan_drifted("s-unknown") is False
