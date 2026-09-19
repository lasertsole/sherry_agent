"""HITL persistent-approval integration tests (P2-2).

Restart simulation (fresh store instance + cleared in-memory register),
operator isolation through the pipeline, and no-operator auto-denial for
approval-requiring gates.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from runtime.session.state_register import state_register_mem

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
from agent.middlewares.humanInTheLoop.approval_scope import NO_OPERATOR_MESSAGE, operator_scope
from agent.middlewares.humanInTheLoop.approval_store import ToolApprovalStore
from agent.middlewares.humanInTheLoop.types import (
    ApprovalDecision,
    BLOCKED_MESSAGE,
)

pytestmark = pytest.mark.unit

_STRATEGIES_MOD = "agent.middlewares.humanInTheLoop.strategies"
_CORE_MOD = "agent.middlewares.humanInTheLoop.core"

_CRON_META = {"origin": "cron", "internal": True}


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _state(sid: str, tool_call: dict, human_meta: dict | None = None) -> dict:
    messages: list = []
    if human_meta is not None:
        messages.append(HumanMessage(content="trigger", metadata=human_meta))
    messages.append(AIMessage(content="", tool_calls=[tool_call]))
    return {"session_id": sid, "messages": messages}


def _middleware(path: Path, **config_kwargs) -> HumanInTheLoop:
    return HumanInTheLoop(HITLConfig(**config_kwargs), approval_store=ToolApprovalStore(path))


def _forbid_interrupt(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    def _boom(value):
        raise AssertionError(f"interrupt() must not be called in {module}")

    monkeypatch.setattr(f"{module}.interrupt", _boom)


def _clear_tool_memory(sid: str, tool_name: str) -> None:
    state_register_mem.delete_state(sid, f"hitl:tool_approved:{tool_name}")


# ────────────────────────────────────────────────────────────────────────────
# interrupt-based tool approvals survive a restart
# ────────────────────────────────────────────────────────────────────────────


def test_interrupted_tool_approval_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "approvals.json"
    sid = "sess-restart-approve"
    monkeypatch.setattr(
        f"{_STRATEGIES_MOD}.interrupt", lambda value: {"decisions": [{"type": "approve"}]}
    )
    first = _middleware(path, interrupted_tools={"bash": True})
    state = _state(sid, _call("bash", {"command": "ls /"}, "c1"))
    assert first.after_model(state, None) is None
    assert state["messages"][-1].tool_calls, "approved call must stay executable"

    _clear_tool_memory(sid, "bash")
    _forbid_interrupt(monkeypatch, _STRATEGIES_MOD)
    restarted = _middleware(path, interrupted_tools={"bash": True})
    state2 = _state(sid, _call("bash", {"command": "ls /"}, "c2"))
    assert restarted.after_model(state2, None) is None
    assert state2["messages"][-1].tool_calls, "persisted approval must skip the re-prompt"


def test_interrupted_tool_denial_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "approvals.json"
    sid = "sess-restart-deny"
    monkeypatch.setattr(
        f"{_STRATEGIES_MOD}.interrupt",
        lambda value: {"decisions": [{"type": "reject", "message": "No way"}]},
    )
    first = _middleware(path, interrupted_tools={"bash": True})
    state = _state(sid, _call("bash", {"command": "ls /"}, "c1"))
    result = first.after_model(state, None)
    first_denial = result["messages"][-1]
    assert first_denial.status == "error"
    assert "No way" in first_denial.content

    _clear_tool_memory(sid, "bash")
    _forbid_interrupt(monkeypatch, _STRATEGIES_MOD)
    restarted = _middleware(path, interrupted_tools={"bash": True})
    state2 = _state(sid, _call("bash", {"command": "ls /"}, "c2"))
    result2 = restarted.after_model(state2, None)
    denial = result2["messages"][-1]
    assert denial.status == "error"
    assert "No way" in denial.content
    assert BLOCKED_MESSAGE in denial.content


# ────────────────────────────────────────────────────────────────────────────
# pipeline-level sessions approvals (explicit API) survive a restart
# ────────────────────────────────────────────────────────────────────────────


def test_pipeline_approval_survives_restart(tmp_path):
    path = tmp_path / "approvals.json"
    sid = "sess-pipe-approve"
    first = ApprovalPipeline(HITLConfig(), MagicMock(), store=ToolApprovalStore(path))
    with operator_scope("alice"):
        first.approve_tool_for_session("bash", {"command": "ls"}, sid)
    state_register_mem.delete_state(sid, "hitl:tool_approved:bash")

    restarted = ApprovalPipeline(HITLConfig(), MagicMock(), store=ToolApprovalStore(path))
    with operator_scope("alice"):
        result = restarted.request_tool_approval("bash", {"command": "ls"}, sid)
    assert result.approved is True
    assert result.decision is ApprovalDecision.SESSION


def test_pipeline_denial_survives_restart(tmp_path):
    path = tmp_path / "approvals.json"
    sid = "sess-pipe-deny"
    first = ApprovalPipeline(HITLConfig(), MagicMock(), store=ToolApprovalStore(path))
    with operator_scope("alice"):
        first.deny_tool_for_session("bash", {"command": "ls"}, sid, reason="User denied: nope")
    state_register_mem.delete_state(sid, "hitl:tool_approved:bash")

    restarted = ApprovalPipeline(HITLConfig(), MagicMock(), store=ToolApprovalStore(path))
    with operator_scope("alice"):
        result = restarted.request_tool_approval("bash", {"command": "ls"}, sid)
    assert result.approved is False
    assert result.decision is ApprovalDecision.DENY
    assert "nope" in result.reason


def test_pipeline_operator_isolation(tmp_path):
    path = tmp_path / "approvals.json"
    sid = "sess-pipe-op"
    pipeline = ApprovalPipeline(HITLConfig(), MagicMock(), store=ToolApprovalStore(path))
    with operator_scope("alice"):
        pipeline.approve_tool_for_session("bash", {"command": "ls"}, sid)
    state_register_mem.delete_state(sid, "hitl:tool_approved:bash")

    with operator_scope("bob"):
        result = pipeline.request_tool_approval("bash", {"command": "ls"}, sid)
    assert result.approved is True
    assert result.decision is ApprovalDecision.ONCE
    with operator_scope("alice"):
        result = pipeline.request_tool_approval("bash", {"command": "ls"}, sid)
    assert result.decision is ApprovalDecision.SESSION


# ────────────────────────────────────────────────────────────────────────────
# no-operator auto-denial (headless turns)
# ────────────────────────────────────────────────────────────────────────────


def test_headless_turn_auto_denies_interrupted_tool(tmp_path, monkeypatch):
    sid = "sess-headless-int"
    _forbid_interrupt(monkeypatch, _STRATEGIES_MOD)
    mw = _middleware(tmp_path / "approvals.json", interrupted_tools={"bash": True})
    state = _state(sid, _call("bash", {"command": "ls /"}, "c1"), human_meta=_CRON_META)

    result = mw.after_model(state, None)

    denial = result["messages"][-1]
    assert denial.status == "error"
    assert NO_OPERATOR_MESSAGE in denial.content
    assert BLOCKED_MESSAGE in denial.content


def test_headless_turn_auto_denies_dangerous_terminal(tmp_path, monkeypatch):
    sid = "sess-headless-term"
    _forbid_interrupt(monkeypatch, _STRATEGIES_MOD)
    mw = _middleware(tmp_path / "approvals.json")
    state = _state(
        sid, _call("terminal", {"command": "git reset --hard"}, "c1"), human_meta=_CRON_META
    )

    result = mw.after_model(state, None)

    denial = result["messages"][-1]
    assert denial.status == "error"
    assert NO_OPERATOR_MESSAGE in denial.content


def test_headless_turn_auto_denies_sandbox_bypass(tmp_path, monkeypatch):
    sid = "sess-headless-sbx"
    _forbid_interrupt(monkeypatch, _CORE_MOD)
    mw = _middleware(tmp_path / "approvals.json")
    state = _state(
        sid,
        _call("terminal", {"command": "echo ok", "sandbox": False}, "c1"),
        human_meta=_CRON_META,
    )

    result = mw.after_model(state, None)

    denial = result["messages"][-1]
    assert denial.status == "error"
    assert NO_OPERATOR_MESSAGE in denial.content


def test_human_turn_still_reaches_the_interrupt(tmp_path, monkeypatch):
    sid = "sess-human-term"
    calls: list = []
    monkeypatch.setattr(
        f"{_STRATEGIES_MOD}.interrupt",
        lambda value: calls.append(value) or {"decisions": [{"type": "approve"}]},
    )
    mw = _middleware(tmp_path / "approvals.json")
    state = _state(sid, _call("terminal", {"command": "git reset --hard"}, "c1"))

    assert mw.after_model(state, None) is None
    assert len(calls) == 1, "a human-driven turn must still raise the approval interrupt"
