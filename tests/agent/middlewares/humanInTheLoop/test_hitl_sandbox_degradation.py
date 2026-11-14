"""A REQUESTED sandbox that would silently degrade must ask the operator.

The rest of the HITL suite assumes a usable OS sandbox (``conftest.py``);
these tests override that assumption and exercise the degradation path:
``sandbox=True`` + ``SANDBOX_POLICY=auto`` + no backend routes through the
existing sandbox-bypass approval instead of running naked.
"""

from __future__ import annotations

from typing import Any, ClassVar

import subprocess

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.middlewares.humanInTheLoop.approval import (
    clear_session_confirm_all,
    clear_session_yolo,
    set_session_confirm_all,
    set_session_yolo,
)
from agent.middlewares.humanInTheLoop.approval_scope import NO_OPERATOR_MESSAGE, operator_scope
from agent.tools.pub_base.env_scrub import SHERRY_SECRET_NAMES
from agent.tools.terminal import build_terminal_tool
from runtime import state_register_mem

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

SESSION = "sess-h3-degradation"
_SENTINEL_TERMINAL = "__TERMINAL_EXECUTED__"
_SENTINEL_REPL = "__REPL_EXECUTED__"


class _ScriptedModel(BaseChatModel):
    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-h3-degradation"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1 and type(self).scripted_calls:
            msg = AIMessage(content="", tool_calls=list(type(self).scripted_calls))
        else:
            msg = AIMessage(content="Done.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


@tool("terminal")
def _fake_terminal(command: str = "", commands: list[str] | None = None) -> str:
    """Stub terminal: proves whether HITL let the call through."""
    return _SENTINEL_TERMINAL


@tool("python_repl")
def _fake_repl(query: str = "") -> str:
    """Stub python_repl: proves whether HITL let the call through."""
    return _SENTINEL_REPL


@pytest.fixture()
def degraded(monkeypatch):
    """Simulate AUTO + no usable backend (the degradation trigger)."""
    import agent.tools.pub_base.sandbox as sandbox

    monkeypatch.setattr(sandbox, "sandbox_degraded", lambda requested=True: bool(requested))
    monkeypatch.setattr(sandbox, "sandbox_effective", lambda requested=True: False)
    for sid in (SESSION,):
        clear_session_yolo(sid)
        clear_session_confirm_all(sid)
        state_register_mem.clear_session(sid)
    yield
    for sid in (SESSION,):
        clear_session_yolo(sid)
        clear_session_confirm_all(sid)
        state_register_mem.clear_session(sid)


def _graph(tool_call: dict[str, Any], target):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = [tool_call]
    return create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[target],
        middleware=[HumanInTheLoop(HITLConfig())],
    )


def _invoke(graph, thread_id: str):
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke(
        {"messages": [HumanMessage(content="please run it")], "session_id": SESSION},
        config,
    )
    return out, config


def _pending(graph, config):
    return list(graph.get_state(config).tasks or [])


def _card_description(graph, config) -> str:
    tasks = _pending(graph, config)
    assert len(tasks) == 1, f"expected one approval card, got {tasks}"
    return tasks[0].interrupts[0].value["action_requests"][0]["description"]


def _tool_messages(out) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


def _terminal_call(call_id: str = "c1", **args: Any) -> dict[str, Any]:
    return {"name": "terminal", "args": {"command": "echo ok", **args}, "id": call_id}


class TestTerminalDegradation:
    def test_the_degraded_call_asks_and_its_card_names_the_missing_sandbox(self, degraded):
        graph = _graph(_terminal_call(), _fake_terminal)

        out, config = _invoke(graph, "t-h3-1")

        description = _card_description(graph, config)
        assert "OS SANDBOX UNAVAILABLE" in description
        assert "unsandboxed" in description
        assert not _tool_messages(out), "nothing may run before the approval"

    def test_approving_runs_the_degraded_call(self, degraded):
        graph = _graph(_terminal_call(), _fake_terminal)
        _, config = _invoke(graph, "t-h3-2")

        out = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

        assert _SENTINEL_TERMINAL in [str(m.content) for m in _tool_messages(out)]

    def test_rejecting_blocks_the_degraded_call(self, degraded):
        graph = _graph(_terminal_call(), _fake_terminal)
        _, config = _invoke(graph, "t-h3-3")

        out = graph.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "no"}]}),
            config,
        )

        contents = [str(m.content) for m in _tool_messages(out)]
        assert _SENTINEL_TERMINAL not in contents
        assert any("denied" in c.lower() for c in contents)

    def test_yolo_skips_the_degraded_approval(self, degraded):
        set_session_yolo(SESSION)
        graph = _graph(_terminal_call(), _fake_terminal)

        out, config = _invoke(graph, "t-h3-4")

        assert _pending(graph, config) == []
        assert _SENTINEL_TERMINAL in [str(m.content) for m in _tool_messages(out)]

    def test_a_headless_turn_auto_denies_the_degraded_call(self, degraded):
        graph = _graph(_terminal_call(), _fake_terminal)

        with operator_scope(None):
            out, config = _invoke(graph, "t-h3-5")

        assert _pending(graph, config) == []
        contents = [str(m.content) for m in _tool_messages(out)]
        assert any(NO_OPERATOR_MESSAGE in c for c in contents)
        assert _SENTINEL_TERMINAL not in contents

    def test_confirm_all_uses_its_own_card_with_the_sandbox_note(self, degraded):
        set_session_confirm_all(SESSION)
        graph = _graph(_terminal_call(), _fake_terminal)

        _, config = _invoke(graph, "t-h3-6")

        description = _card_description(graph, config)
        assert "Confirm-all mode" in description
        assert "sandbox unavailable — runs unsandboxed" in description

    def test_a_usable_backend_never_asks(self, degraded, monkeypatch):
        import agent.tools.pub_base.sandbox as sandbox

        monkeypatch.setattr(sandbox, "sandbox_degraded", lambda requested=True: False)
        monkeypatch.setattr(sandbox, "sandbox_effective", lambda requested=True: True)
        graph = _graph(_terminal_call(), _fake_terminal)

        out, config = _invoke(graph, "t-h3-7")

        assert _pending(graph, config) == []
        assert _SENTINEL_TERMINAL in [str(m.content) for m in _tool_messages(out)]


class TestPythonReplDegradation:
    def test_the_degraded_repl_call_asks(self, degraded):
        graph = _graph(
            {"name": "python_repl", "args": {"query": "print(1)"}, "id": "r1"}, _fake_repl
        )

        out, config = _invoke(graph, "t-h3-8")

        assert "OS SANDBOX UNAVAILABLE" in _card_description(graph, config)
        assert not _tool_messages(out)

    def test_approving_runs_the_degraded_repl_call(self, degraded):
        graph = _graph(
            {"name": "python_repl", "args": {"query": "print(1)"}, "id": "r2"}, _fake_repl
        )
        _, config = _invoke(graph, "t-h3-9")

        out = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

        assert _SENTINEL_REPL in [str(m.content) for m in _tool_messages(out)]


class TestExplicitBypassCardUnchanged:
    def test_sandbox_false_keeps_the_bypass_wording(self, degraded):
        graph = _graph(_terminal_call(sandbox=False), _fake_terminal)

        _, config = _invoke(graph, "t-h3-10")

        description = _card_description(graph, config)
        assert "Sandbox bypass approval" in description
        assert "OS SANDBOX UNAVAILABLE" not in description


class _FakeShellProc:
    """Fake Popen: records the spawn kwargs, never executes anything."""

    returncode = 0

    def __init__(self, recorder: list[dict[str, Any]], *args: Any, **kwargs: Any):
        recorder.append({"args": args, "kwargs": kwargs})

    def communicate(self, timeout=None):
        return (b"ok-out", b"")

    def kill(self):
        pass


class TestRealToolDegradedApprovalE2E:
    def test_approved_degraded_terminal_runs_unsandboxed_with_scrubbed_env(
        self, degraded, monkeypatch
    ):
        """Windows-shaped host: card -> approve -> real tool runs unsandboxed."""
        monkeypatch.setenv("MAIN_LLM_API_KEY", "sk-fake-secret")
        monkeypatch.setattr("agent.tools.terminal.core.get_backend", lambda _policy: None)
        popen_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _FakeShellProc(popen_calls, *a, **kw)
        )

        graph = _graph(
            {
                "name": "terminal",
                "args": {"commands": ["echo ok"]},
                "id": "h3-e2e",
            },
            build_terminal_tool(),
        )
        _out, config = _invoke(graph, "t-h3-e2e")

        description = _card_description(graph, config)
        assert "OS SANDBOX UNAVAILABLE" in description
        assert popen_calls == [], "nothing may spawn before the approval"

        graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

        assert _pending(graph, config) == []
        assert popen_calls, "the approved degraded call must reach the tool layer"
        kwargs = popen_calls[0]["kwargs"]
        assert kwargs.get("shell") is True, "degraded path is the unsandboxed shell spawn"
        env = kwargs.get("env") or {}
        for name in SHERRY_SECRET_NAMES:
            assert name not in env
        assert "MAIN_LLM_API_KEY" not in env
