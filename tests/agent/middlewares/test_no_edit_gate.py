"""Eval-only no-edit gate: warn, harden, block research, reset on write."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.no_edit_gate import NoEditGate
from runtime import StateKey, state_register_mem

pytestmark = [pytest.mark.unit]

_SESSION = "evals-swe-test-no-edit-gate"


def _request(tool_name: str, **args: object) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": "call-1"},
        state={"session_id": _SESSION},
        tool=None,
    )


@pytest.mark.asyncio
async def test_gate_warns_at_the_call_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERRY_EVAL_REQUIRE_EDIT", "1")
    gate = NoEditGate()
    state = {"session_id": _SESSION}
    gate._before_agent_impl(state)
    state_register_mem.set_state(_SESSION, StateKey.ITERATION_BUDGET, 90)
    state_register_mem.set_state(_SESSION, StateKey.ITERATION_BUDGET_USED, 30)

    for _ in range(14):
        assert gate._precheck(_request("read_file", file_path="x")) is None
    out = await gate.abefore_model(state)
    assert out is None, "no warning before 15 calls"
    assert gate._precheck(_request("read_file", file_path="x")) is None
    out = await gate.abefore_model(state)
    assert out is not None
    assert "NO-EDIT GATE" in out["messages"][0].content


@pytest.mark.asyncio
async def test_gate_hardens_blocks_research_and_resets_on_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHERRY_EVAL_REQUIRE_EDIT", "1")
    gate = NoEditGate()
    state = {"session_id": _SESSION}
    gate._before_agent_impl(state)
    state_register_mem.set_state(_SESSION, StateKey.ITERATION_BUDGET, 90)
    state_register_mem.set_state(_SESSION, StateKey.ITERATION_BUDGET_USED, 63)

    out = await gate.abefore_model(state)
    assert out is not None
    assert "FINAL" in out["messages"][0].content

    blocked = gate._precheck(_request("terminal", commands=["ls"]))
    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    assert gate._precheck(_request("read_file", file_path="x")) is None
    assert gate._precheck(_request("patch_file", file_path="x")) is None
    # A write satisfies the gate; the research tools are usable again.
    assert gate._precheck(_request("terminal", commands=["pytest"])) is None


def test_inactive_sessions_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERRY_EVAL_REQUIRE_EDIT", "1")
    gate = NoEditGate()
    other = SimpleNamespace(
        tool_call={"name": "read_file", "args": {"file_path": "x"}, "id": "call-2"},
        state={"session_id": "agent:main:subagent:abc"},
        tool=None,
    )
    gate._before_agent_impl(other.state)
    for _ in range(20):
        assert gate._precheck(other) is None
