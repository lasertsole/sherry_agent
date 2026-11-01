"""Eval-only delegate-only gate: filter the main face, refuse blocked tools."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.delegate_only_gate import DelegateOnlyGate

pytestmark = [pytest.mark.unit]

_SESSION = "evals-swe-test-delegate"
_TOOLS = ("read_file", "sessions_spawn", "write_file", "subagents_list")


def _request(session_id: str = _SESSION, tool_name: str = "terminal") -> SimpleNamespace:
    return SimpleNamespace(
        tools=[SimpleNamespace(name=name) for name in _TOOLS],
        state={"session_id": session_id},
        tool_call={"name": tool_name, "args": {}, "id": "call-1"},
    )


def _apply(request: SimpleNamespace) -> SimpleNamespace:
    class _Request:
        def __init__(self) -> None:
            self.tools = request.tools
            self.state = request.state

        def override(self, **kwargs: object) -> _Request:
            out = _Request()
            out.tools = kwargs["tools"]
            return out

    return DelegateOnlyGate()._apply(_Request())  # type: ignore[return-value]


def test_filters_exploration_and_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERRY_EVAL_DELEGATE_ONLY", "1")
    out = _apply(_request())
    assert [tool.name for tool in out.tools] == ["sessions_spawn", "subagents_list"]


def test_refuses_a_blocked_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERRY_EVAL_DELEGATE_ONLY", "1")
    refusal = DelegateOnlyGate()._refusal(_request(tool_name="read_file"))
    assert isinstance(refusal, ToolMessage)
    assert refusal.status == "error"
    assert "sessions_spawn" in str(refusal.content)


def test_child_sessions_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHERRY_EVAL_DELEGATE_ONLY", "1")
    out = _apply(_request(session_id="agent:main:subagent:abc"))
    assert [tool.name for tool in out.tools] == list(_TOOLS)
    assert DelegateOnlyGate()._refusal(_request(session_id="agent:main:subagent:abc")) is None
