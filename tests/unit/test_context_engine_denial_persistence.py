"""Unit tests for HITL denial persistence re-pairing.

Covers:
- ``_reconcile_denials_for_persistence`` (agent/middlewares/context_engine/core.py)
  — re-attaches denied tool calls (stripped by ``HumanInTheLoop.after_model``) onto
  a copy of the preceding AIMessage so the denial ToolMessage survives
  ``sanitize_tool_use_result_pairing`` and is persisted to MesMemory.
- Interaction with ``sanitize_tool_use_result_pairing`` — once re-paired, the
  denial pair survives the sanitizer's orphan cleanup.
- Guarantees: graph-state message objects are never mutated; real (non-error)
  orphaned results keep being dropped; already-paired messages are untouched.
"""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pub_func import sanitize_tool_use_result_pairing
from agent.middlewares.context_engine.core import _reconcile_denials_for_persistence


pytestmark = pytest.mark.unit


def _denial(tool_call_id: str = "call_reject_1", name: str = "terminal") -> ToolMessage:
    """Denial ToolMessage as produced by HumanInTheLoop.after_model."""
    return ToolMessage(
        content="User denied: nope. The user has NOT consented to this action.",
        name=name,
        tool_call_id=tool_call_id,
        status="error",
    )


def _stripped_ai() -> AIMessage:
    """AIMessage shape after HumanInTheLoop.after_model strips the rejected call."""
    return AIMessage(content="let me run that", tool_calls=[])


class TestReconcileDenials:
    def test_full_reject_pair_survives_sanitize(self):
        """The E2E bug: orphaned denial must survive sanitize after re-pairing."""
        msgs = [HumanMessage("hi"), _stripped_ai(), _denial()]
        repaired = sanitize_tool_use_result_pairing(
            _reconcile_denials_for_persistence(msgs)
        )
        ai = [m for m in repaired if isinstance(m, AIMessage)]
        tools = [m for m in repaired if isinstance(m, ToolMessage)]
        assert len(ai) == 1
        assert len(tools) == 1
        assert ai[0].tool_calls and ai[0].tool_calls[0]["id"] == "call_reject_1"
        assert ai[0].tool_calls[0]["name"] == "terminal"
        assert tools[0].tool_call_id == "call_reject_1"
        assert tools[0].status == "error"
        assert "User denied" in tools[0].content

    def test_without_reconcile_sanitize_drops_denial(self):
        """Documents the pre-fix behaviour: the orphaned denial is dropped."""
        msgs = [HumanMessage("hi"), _stripped_ai(), _denial()]
        repaired = sanitize_tool_use_result_pairing(msgs)
        assert not [m for m in repaired if isinstance(m, ToolMessage)]

    def test_original_state_object_not_mutated(self):
        """Re-pairing must work on copies — the checkpoint state is untouched."""
        ai = _stripped_ai()
        denial = _denial()
        _reconcile_denials_for_persistence([HumanMessage("hi"), ai, denial])
        assert ai.tool_calls == []
        assert denial.tool_call_id == "call_reject_1"

    def test_mixed_case_denial_and_real_result_both_survive(self):
        """Approve+reject mix: real result keeps its call, denial is re-attached."""
        ai = AIMessage(
            content="working",
            tool_calls=[
                {"name": "read", "args": {"path": "a.md"}, "id": "call_ok", "type": "tool_call"}
            ],
        )
        real = ToolMessage(content="file contents", name="read", tool_call_id="call_ok")
        denial = _denial("call_reject_1")
        repaired = sanitize_tool_use_result_pairing(
            _reconcile_denials_for_persistence([HumanMessage("hi"), ai, denial, real])
        )
        tools = [m for m in repaired if isinstance(m, ToolMessage)]
        assert {t.tool_call_id for t in tools} == {"call_ok", "call_reject_1"}
        ai_out = [m for m in repaired if isinstance(m, AIMessage)][0]
        assert {c["id"] for c in ai_out.tool_calls} == {"call_ok", "call_reject_1"}
        # contents must survive verbatim (guards against placeholder substitution)
        by_tool = {t.tool_call_id: t for t in tools}
        assert "User denied" in by_tool["call_reject_1"].content
        assert by_tool["call_ok"].content == "file contents"
        # the approved call keeps its real args; the denial gets the synthetic {}
        by_id = {c["id"]: c for c in ai_out.tool_calls}
        assert by_id["call_ok"]["args"] == {"path": "a.md"}
        assert by_id["call_reject_1"]["args"] == {}

    def test_paired_error_tool_message_not_duplicated(self):
        """An error ToolMessage whose call still exists must not get a synthetic dup."""
        ai = AIMessage(
            content="working",
            tool_calls=[
                {"name": "terminal", "args": {"commands": "ls"}, "id": "call_x", "type": "tool_call"}
            ],
        )
        err = ToolMessage(content="boom", name="terminal", tool_call_id="call_x", status="error")
        repaired = sanitize_tool_use_result_pairing(
            _reconcile_denials_for_persistence([HumanMessage("hi"), ai, err])
        )
        ai_out = [m for m in repaired if isinstance(m, AIMessage)][0]
        assert [c["id"] for c in ai_out.tool_calls] == ["call_x"]
        assert len([m for m in repaired if isinstance(m, ToolMessage)]) == 1

    def test_non_error_orphan_not_repaired(self):
        """Real results orphaned by trimming keep being dropped (no fabricated history)."""
        msgs = [
            HumanMessage("hi"),
            _stripped_ai(),
            ToolMessage(content="data", name="read", tool_call_id="call_orphan"),
        ]
        repaired = sanitize_tool_use_result_pairing(
            _reconcile_denials_for_persistence(msgs)
        )
        assert not [m for m in repaired if isinstance(m, ToolMessage)]

    def test_empty_error_orphan_ignored(self):
        """Empty error ToolMessages are left for the sanitizer to drop."""
        msgs = [
            HumanMessage("hi"),
            _stripped_ai(),
            ToolMessage(content="", name="terminal", tool_call_id="call_e", status="error"),
        ]
        out = _reconcile_denials_for_persistence(msgs)
        assert out[1].tool_calls == []

    def test_no_orphans_returns_input_untouched(self):
        """Fast path: nothing orphaned → the input list is returned as-is."""
        ai = AIMessage(
            content="hi",
            tool_calls=[{"name": "read", "args": {}, "id": "call_ok", "type": "tool_call"}],
        )
        real = ToolMessage(content="ok", name="read", tool_call_id="call_ok")
        msgs = [HumanMessage("u"), ai, real]
        out = _reconcile_denials_for_persistence(msgs)
        assert out is msgs
