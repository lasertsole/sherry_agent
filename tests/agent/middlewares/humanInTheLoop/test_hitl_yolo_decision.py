"""HITL "yolo" resume decision tests.

Covers the session-scoped YOLO feature: answering an approval interrupt with
``Command(resume={"decisions": [{"type": "yolo"}]})`` approves ALL pending
action requests of that interrupt AND activates the session-scoped flag
(``hitl:session_yolo`` in ``state_register_mem``), so subsequent approval
checks in the SAME session skip interrupts exactly like
``HITLConfig.yolo_mode=True`` would. The flag is per-session and lives in the
in-memory register (cleared with the session) — distinct from the persistent
external-path YOLO in ``state_register_db``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from runtime.state_register import state_register_mem

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.middlewares.humanInTheLoop.approval import (
    ApprovalPipeline,
    is_yolo_mode,
    set_session_yolo,
)

pytestmark = pytest.mark.unit

_STRATEGIES_MOD = "agent.middlewares.humanInTheLoop.strategies"
_CORE_MOD = "agent.middlewares.humanInTheLoop.core"


def _clean_state(session_id: str, *keys: str):
    for key in keys:
        state_register_mem.delete_state(session_id, f"hitl:{key}")


def _flag(sid: str) -> bool:
    return bool(state_register_mem.get_state(sid, "hitl:session_yolo", False))


def _ai_state(sid: str, tool_call: dict) -> dict:
    return {
        "session_id": sid,
        "messages": [AIMessage(content="", tool_calls=[tool_call])],
    }


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _forbid_interrupt(monkeypatch: pytest.MonkeyPatch, module: str):
    def _boom(value):
        raise AssertionError(f"interrupt() must not be called in {module} when YOLO flag is set")

    monkeypatch.setattr(f"{module}.interrupt", _boom)


# ────────────────────────────────────────────────────────────────────────────
# yolo decision: terminal dangerous-command interrupt
# ────────────────────────────────────────────────────────────────────────────


def test_terminal_yolo_decision_approves_and_sets_flag(monkeypatch, unit_test_config):
    """A yolo resume approves the pending dangerous command and flips the flag."""
    sid = "sess-yolo-term"
    _clean_state(sid, "session_yolo", "session_approved", "permanent")

    def _fake_interrupt(value):
        assert value["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]
        return {"decisions": [{"type": "yolo"}]}

    monkeypatch.setattr(f"{_STRATEGIES_MOD}.interrupt", _fake_interrupt)
    mw = HumanInTheLoop(HITLConfig())
    state = _ai_state(sid, _call("terminal", {"command": "git push --force origin main"}, "c1"))

    result = mw.after_model(state, None)

    assert result is None  # no artificial deny ToolMessage → call approved
    assert state["messages"][0].tool_calls  # tool call kept for execution
    assert _flag(sid) is True

    _clean_state(sid, "session_yolo")


def test_session_yolo_flag_skips_subsequent_terminal_interrupt(monkeypatch, unit_test_config):
    """After the flag is set, a dangerous command is approved with NO interrupt."""
    sid = "sess-yolo-skip"
    _clean_state(sid, "session_yolo", "session_approved", "permanent")
    set_session_yolo(sid)
    _forbid_interrupt(monkeypatch, _STRATEGIES_MOD)

    mw = HumanInTheLoop(HITLConfig())
    state = _ai_state(sid, _call("terminal", {"command": "git push --force origin main"}, "c2"))

    result = mw.after_model(state, None)

    assert result is None
    assert state["messages"][0].tool_calls

    _clean_state(sid, "session_yolo")


def test_session_yolo_flag_is_per_session(unit_test_config):
    """The flag set for one session does not leak into another session."""
    set_session_yolo("sess-yolo-a")
    assert _flag("sess-yolo-a") is True
    assert _flag("sess-yolo-b") is False
    assert is_yolo_mode(HITLConfig(), "sess-yolo-a") is True
    assert is_yolo_mode(HITLConfig(), "sess-yolo-b") is False

    _clean_state("sess-yolo-a", "session_yolo")


# ────────────────────────────────────────────────────────────────────────────
# yolo decision: interrupt_on config tools
# ────────────────────────────────────────────────────────────────────────────


def test_interrupt_on_yolo_decision_approves_and_sets_flag(monkeypatch, unit_test_config):
    """A configured interrupted tool answered with yolo approves and sets the flag."""
    sid = "sess-yolo-int"
    _clean_state(sid, "session_yolo")

    monkeypatch.setattr(
        f"{_STRATEGIES_MOD}.interrupt",
        lambda value: {"decisions": [{"type": "yolo"}]},
    )
    mw = HumanInTheLoop(HITLConfig(interrupted_tools={"bash": True}))
    state = _ai_state(sid, _call("bash", {"command": "ls /"}, "c3"))

    result = mw.after_model(state, None)

    assert result is None
    assert state["messages"][0].tool_calls
    assert _flag(sid) is True

    _clean_state(sid, "session_yolo")


# ────────────────────────────────────────────────────────────────────────────
# yolo decision: sandbox-bypass interrupt
# ────────────────────────────────────────────────────────────────────────────


def test_sandbox_bypass_yolo_decision_approves_and_sets_flag(monkeypatch, unit_test_config):
    """A sandbox=False terminal call answered with yolo approves and sets the flag."""
    sid = "sess-yolo-sbx"
    _clean_state(sid, "session_yolo")

    monkeypatch.setattr(
        f"{_CORE_MOD}.interrupt",
        lambda value: {"decisions": [{"type": "yolo"}]},
    )
    mw = HumanInTheLoop(HITLConfig())
    approved, deny_msg = mw._sandbox_bypass_interrupt(
        _call("terminal", {"command": "whoami", "sandbox": False}, "c4"),
        "terminal",
        "Command: whoami",
        sid,
    )

    assert approved is True
    assert deny_msg is None
    assert _flag(sid) is True

    _clean_state(sid, "session_yolo")


def test_session_yolo_flag_skips_sandbox_bypass_interrupt(monkeypatch, unit_test_config):
    """With the flag set, a sandbox=False terminal call never reaches the interrupt."""
    sid = "sess-yolo-sbx-skip"
    _clean_state(sid, "session_yolo", "session_approved", "permanent")
    set_session_yolo(sid)
    _forbid_interrupt(monkeypatch, _STRATEGIES_MOD)

    mw = HumanInTheLoop(HITLConfig())
    state = _ai_state(sid, _call("terminal", {"command": "whoami", "sandbox": False}, "c5"))

    result = mw.after_model(state, None)

    assert result is None
    assert state["messages"][0].tool_calls

    _clean_state(sid, "session_yolo")


# ────────────────────────────────────────────────────────────────────────────
# session flag → approval pipeline layer 3
# ────────────────────────────────────────────────────────────────────────────


def test_check_command_layer3_honors_session_flag(unit_test_config):
    """The session flag bypasses dangerous-pattern escalation in check_command."""
    sid = "sess-yolo-pipe"
    _clean_state(sid, "session_yolo", "session_approved", "permanent")
    pipeline = ApprovalPipeline(HITLConfig(), MagicMock())
    assert pipeline.check_command("git push --force origin main", sid).approved is False

    set_session_yolo(sid)
    result = pipeline.check_command("git push --force origin main", sid)
    assert result.approved is True
    assert "YOLO" in result.reason

    _clean_state(sid, "session_yolo")


# ────────────────────────────────────────────────────────────────────────────
# yolo decision: clarify interrupt
# ────────────────────────────────────────────────────────────────────────────


def test_clarify_yolo_returns_message_and_sets_flag(monkeypatch, unit_test_config):
    """clarify() answered with yolo returns the message and activates the flag."""
    sid = "sess-yolo-clarify"
    _clean_state(sid, "session_yolo")

    monkeypatch.setattr(
        f"{_CORE_MOD}.interrupt",
        lambda value: {"decisions": [{"type": "yolo", "message": "继续"}]},
    )
    mw = HumanInTheLoop(HITLConfig())

    assert mw.clarify("继续吗?", ["是", "否"], sid) == "继续"
    assert _flag(sid) is True

    _clean_state(sid, "session_yolo")
