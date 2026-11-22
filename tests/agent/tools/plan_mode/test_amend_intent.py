"""Unit tests for ``amend_intent`` (model-correctable intent, with an audit trail).

Chosen policy: the model MAY correct a misjudged intent directly — no user
approval, no forced reviewer re-check — but the correction is recorded in the
anchor and takes effect for the next todo reminder and verify run.
"""

from __future__ import annotations

import pytest

import agent.tools.plan_mode.amend_intent as amend_mod
import agent.tools.plan_mode.intent_anchor as anchor_mod
from agent.tools.plan_mode import build_plan_mode_tools
from config.features import PLAN_MODE
from runtime import StateKey

pytestmark = [pytest.mark.unit]

ANCHOR = {
    "goal": "Original goal that was misjudged",
    "must_have": ["old must 1"],
    "must_not_have": ["old boundary"],
    "acceptance": ["old criterion"],
    "revision": "rev123",
    "source_ref": "/tmp/plan-rev123.md",
}


class _FakeRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.data[(session_id, str(key))] = value
        return True

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, str(key)), default)


@pytest.fixture()
def registers(monkeypatch):
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(anchor_mod, "state_register_mem", mem)
    monkeypatch.setattr(anchor_mod, "state_register_db", db)
    monkeypatch.setitem(PLAN_MODE, "enabled", True)
    return {"mem": mem, "db": db}


def _seed(anchor: dict | None) -> None:
    if anchor is None:
        return
    anchor_mod.write_intent_anchor("s1", anchor)


class TestAmend:
    def test_refuses_without_an_anchor(self, registers):
        out = amend_mod.build_amend_intent_tool()._run(
            goal="new goal", reason="I misread the request", session_id="s1"
        )

        assert "No approved intent anchor" in out
        assert anchor_mod.read_intent_anchor("s1") is None

    def test_amends_goal_and_records_audit(self, registers):
        _seed(ANCHOR)

        out = amend_mod.build_amend_intent_tool()._run(
            goal="Corrected goal",
            reason="The user actually asked for fail-closed behavior",
            session_id="s1",
        )

        anchor = anchor_mod.read_intent_anchor("s1")
        assert "Intent anchor amended" in out
        assert anchor is not None and anchor["goal"] == "Corrected goal"
        assert len(anchor["amendments"]) == 1
        entry = anchor["amendments"][0]
        assert entry["actor"] == "model"
        assert entry["from_goal"] == ANCHOR["goal"]
        assert entry["to_goal"] == "Corrected goal"
        assert "fail-closed" in entry["reason"]
        assert entry["at"]  # ISO timestamp

    def test_omitted_fields_are_kept_provided_fields_replaced(self, registers):
        _seed(ANCHOR)

        amend_mod.build_amend_intent_tool()._run(
            goal="g2",
            reason="r2",
            must_have=["new must"],
            session_id="s1",
        )

        anchor = anchor_mod.read_intent_anchor("s1")
        assert anchor is not None
        assert anchor["must_have"] == ["new must"]
        assert anchor["must_not_have"] == ANCHOR["must_not_have"]
        assert anchor["acceptance"] == ANCHOR["acceptance"]

    def test_revision_and_plan_ref_are_preserved(self, registers, tmp_path):
        """A corrected intent does not count as a plan-file drift."""
        path = tmp_path / "plan.md"
        path.write_text("## Goal\noriginal\n", encoding="utf-8")
        seed = dict(ANCHOR, source_ref=str(path))
        _seed(seed)

        amend_mod.build_amend_intent_tool()._run(
            goal="corrected", reason="misread", session_id="s1"
        )

        anchor = anchor_mod.read_intent_anchor("s1")
        assert anchor is not None
        assert anchor["revision"] == ANCHOR["revision"]
        assert anchor["source_ref"] == str(path)

    def test_reason_is_mandatory(self, registers):
        _seed(ANCHOR)

        out = amend_mod.build_amend_intent_tool()._run(goal="g", reason="", session_id="s1")

        assert "reason is required" in out
        anchor = anchor_mod.read_intent_anchor("s1")
        assert anchor is not None and anchor["goal"] == ANCHOR["goal"]

    def test_audit_trail_accumulates(self, registers):
        _seed(ANCHOR)
        tool = amend_mod.build_amend_intent_tool()

        tool._run(goal="g2", reason="r1", session_id="s1")
        tool._run(goal="g3", reason="r2", session_id="s1")

        anchor = anchor_mod.read_intent_anchor("s1")
        assert anchor is not None and len(anchor["amendments"]) == 2

    def test_rendered_anchor_shows_amendment_hint(self, registers):
        _seed(ANCHOR)
        amend_mod.build_amend_intent_tool()._run(
            goal="corrected", reason="misread", session_id="s1"
        )

        from workspace.intent_anchor_view import format_intent_anchor

        text = format_intent_anchor(anchor_mod.read_intent_anchor("s1") or {})
        assert "amended 1x" in text
        assert "amend_intent(goal, reason)" in text
        assert "enter_plan_mode(replan=True)" in text


class TestRegistration:
    def test_tool_is_registered_with_the_plan_mode_family(self, registers):
        names = [tool.name for tool in build_plan_mode_tools()]

        assert "enter_plan_mode" in names
        assert "exit_plan_mode" in names
        assert "amend_intent" in names

    def test_disabled_plan_mode_registers_no_intent_tools(self, registers, monkeypatch):
        monkeypatch.setitem(PLAN_MODE, "enabled", False)

        assert build_plan_mode_tools() == []

    def test_anchor_state_key_is_declared(self):
        assert StateKey.PLAN_INTENT_ANCHOR == "plan_intent_anchor"
