"""Plan mode's read-only tool gate (``PlanModeToolGate``).

The per-session tool selection was removed with the preset panel's tool tab, but
plan mode's whitelist must survive: while a plan awaits approval the model is
only OFFERED the read-only tools and a write call that still arrives is REFUSED
at execution. These tests drive both hooks directly.
"""

from __future__ import annotations

import pytest

from agent.middlewares.plan_mode_gate import PlanModeToolGate
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.unit]

SESSION = "sess-plan-mode-gate"


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Request:
    """Minimal ModelRequest double: state + tools + an override() that copies."""

    def __init__(self, state: dict, tools: list[_Tool]) -> None:
        self.state = state
        self.tools = list(tools)
        self.overrides: list[dict] = []

    def override(self, **kwargs):
        clone = _Request(self.state, kwargs.get("tools", self.tools))
        clone.overrides = [*self.overrides, kwargs]
        return clone


class _ToolCallRequest:
    def __init__(self, session_id: str, name: str, call_id: str = "call-1") -> None:
        self.state = {"session_id": session_id}
        self.tool_call = {"name": name, "id": call_id, "args": {}}


@pytest.fixture(autouse=True)
def _isolated_plan_state():
    state_register_mem.clear_session(SESSION)
    yield
    state_register_mem.clear_session(SESSION)


def _enter_plan_mode(status: str = "planning") -> None:
    state_register_mem.set_state(SESSION, StateKey.PLAN_STATUS, status)


def _model_call_names(request: _Request) -> list[str]:
    seen: dict = {}

    def handler(req):
        seen["names"] = [getattr(tool, "name", None) for tool in req.tools]
        return req

    PlanModeToolGate().wrap_model_call(request, handler)
    return seen["names"]


def test_plan_mode_narrows_the_face_to_the_read_only_whitelist():
    _enter_plan_mode()
    request = _Request(
        {"session_id": SESSION},
        [
            _Tool(name)
            for name in (
                "read_file",
                "search_files",
                "write_file",
                "terminal",
                "question",
                "exit_plan_mode",
            )
        ],
    )

    names = _model_call_names(request)

    assert "read_file" in names and "search_files" in names
    assert "question" in names and "exit_plan_mode" in names
    assert "write_file" not in names and "terminal" not in names


def test_outside_plan_mode_the_full_face_is_untouched():
    request = _Request({"session_id": SESSION}, [_Tool(n) for n in ("read_file", "write_file")])

    assert _model_call_names(request) == ["read_file", "write_file"]


def test_plan_mode_refuses_a_write_tool_at_execution():
    _enter_plan_mode()
    refusal = PlanModeToolGate().wrap_tool_call(
        _ToolCallRequest(SESSION, "write_file"), lambda _r: "ran"
    )

    assert getattr(refusal, "status", None) == "error"
    assert "Plan mode is active" in refusal.content


def test_plan_mode_allows_a_read_tool_at_execution():
    _enter_plan_mode()

    assert (
        PlanModeToolGate().wrap_tool_call(_ToolCallRequest(SESSION, "read_file"), lambda _r: "ran")
        == "ran"
    )


def test_an_approved_plan_frees_the_write_tools_again():
    _enter_plan_mode("ready")
    middleware = PlanModeToolGate()

    request = _Request({"session_id": SESSION}, [_Tool(n) for n in ("read_file", "write_file")])
    assert _model_call_names(request) == ["read_file", "write_file"]
    assert (
        middleware.wrap_tool_call(_ToolCallRequest(SESSION, "write_file"), lambda _r: "ran")
        == "ran"
    )


def test_a_broken_plan_read_is_fail_open(monkeypatch):
    """A register failure must never leave a session stuck in a narrow face."""
    import agent.tools.plan_mode.state as plan_state

    def _boom(*_a, **_k):
        raise RuntimeError("register down")

    monkeypatch.setattr(plan_state, "read_plan_status", _boom)
    request = _Request({"session_id": SESSION}, [_Tool(n) for n in ("read_file", "write_file")])

    assert _model_call_names(request) == ["read_file", "write_file"]
