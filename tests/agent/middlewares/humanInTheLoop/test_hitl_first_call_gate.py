"""File-change approval gate: auto_edit is silent, the ask-once reminder is opt-in.

Contract under test (2026-10-09, the user's call): under the default access
mode ("自动编辑" / auto_edit) a file change NEVER raises an approval card — the
agent edits, and only dangerous or uncertain calls ask. What remains here:

* the default config runs ``write_file`` without any interrupt;
* ``first_call_confirmation_enabled=True`` restores an optional ask-once
  reminder: the first use of a listed tool per session asks, an approval is
  remembered, a rejection is not;
* "变更前确认" / confirm_all asks on EVERY change regardless of that flag
  (pinned in ``test_hitl_confirm_all_mode.py``);
* YOLO bypasses; tools outside the list are untouched; a turn with no operator
  in scope (cron / heartbeat / subagent carrier) is left to its existing
  policy instead of suspending on an unanswerable prompt.

Covered on a REAL ``create_agent`` graph driven by a scripted model: a gated
call parks a pending ``HumanInTheLoop.after_model`` interrupt (LangGraph parks
it on the thread instead of raising out of ``invoke()``).
"""

from __future__ import annotations

from typing import Any, ClassVar

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

from agent.middlewares.humanInTheLoop import (
    BLOCKED_MESSAGE,
    HITLConfig,
    HumanInTheLoop,
)
from agent.middlewares.humanInTheLoop.types import HITL_CONFIRMED_TOOLS_KEY
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.module]

_SENTINEL = "__FILE_WRITTEN__"

#: The opt-in ask-once reminder, for the tests that exercise the mechanism.
_REMINDER_ON = HITLConfig(first_call_confirmation_enabled=True)


@tool("write_file")
def _write_file(path: str, content: str = "") -> str:
    """Fake file write (the gate only needs a listed, side-effecting tool)."""
    return f"{_SENTINEL}:{path}:{len(content)}"


@tool("read_file")
def _read_file(path: str) -> str:
    """Read-only tool: never gated."""
    return f"read:{path}"


class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-hitl-first-call"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1 and type(self).scripted_calls:
            msg = AIMessage(content="", tool_calls=list(type(self).scripted_calls))
        else:
            msg = AIMessage(content="Done (no further tool calls).")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


def _call(tool_call_id: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": tool_call_id, "name": name, "args": args or {}, "type": "tool_call"}


def _build_graph(scripted_calls: list[dict[str, Any]], hitl_config: HITLConfig | None = None):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)
    hitl = HumanInTheLoop(hitl_config or HITLConfig())
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[_write_file, _read_file],
        middleware=[hitl],
    )
    return graph, hitl


def _invoke(graph, thread_id: str, session_id: str) -> tuple[dict, RunnableConfig]:
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke(
        {"messages": [HumanMessage(content="please write the file")], "session_id": session_id},
        config,
    )
    return out, config


def _pending_tasks(graph, config: RunnableConfig):
    """Interrupts parked on the thread (the resume contract's entry point)."""
    return list(graph.get_state(config).tasks or [])


def _resume(graph, config: RunnableConfig, decision: str, message: str | None = None):
    entry: dict[str, Any] = {"type": decision}
    if message is not None:
        entry["message"] = message
    return graph.invoke(Command(resume={"decisions": [entry]}), config)


def _confirmed(session_id: str) -> list[str]:
    return list(state_register_mem.get_state(session_id, HITL_CONFIRMED_TOOLS_KEY, []) or [])


@pytest.fixture(autouse=True)
def _clean_register():
    """Each test starts with no remembered confirmations."""
    state_register_mem.set_state("sess-first-call", HITL_CONFIRMED_TOOLS_KEY, [])
    yield
    state_register_mem.set_state("sess-first-call", HITL_CONFIRMED_TOOLS_KEY, [])


def test_default_mode_never_asks_for_a_file_change():
    """auto_edit means auto edit: the write runs, no card, nothing remembered."""
    graph, _ = _build_graph([_call("c1", "write_file", {"path": "notes.txt"})])

    out, _ = _invoke(graph, "t-default", "sess-first-call")

    assert _pending_tasks(graph, {"configurable": {"thread_id": "t-default"}}) == []
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert any(_SENTINEL in m.content for m in tool_messages)
    assert _confirmed("sess-first-call") == []


def test_first_call_interrupts_and_approval_runs_the_tool_when_the_reminder_is_on():
    graph, _ = _build_graph([_call("c1", "write_file", {"path": "notes.txt"})], _REMINDER_ON)
    config: RunnableConfig = {"configurable": {"thread_id": "t-approve"}}

    out, _ = _invoke(graph, "t-approve", "sess-first-call")

    # Parked on the thread, nothing executed yet.
    tasks = _pending_tasks(graph, config)
    assert len(tasks) == 1, f"expected 1 pending interrupt, got {len(tasks)}"
    assert tasks[0].name == "HumanInTheLoop.after_model"
    request = tasks[0].interrupts[0].value
    assert request["action_requests"][0]["name"] == "write_file"
    assert request["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]
    assert not [m for m in out["messages"] if isinstance(m, ToolMessage)]

    out = _resume(graph, config, "approve")
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert any(_SENTINEL in m.content for m in tool_messages)
    assert _confirmed("sess-first-call") == ["write_file"]


def test_second_call_of_a_confirmed_tool_does_not_interrupt():
    graph, _ = _build_graph([_call("c1", "write_file", {"path": "a.txt"})], _REMINDER_ON)
    config: RunnableConfig = {"configurable": {"thread_id": "t-second"}}
    _invoke(graph, "t-second", "sess-first-call")
    assert len(_pending_tasks(graph, config)) == 1
    _resume(graph, config, "approve")

    # Same session, same tool, new thread: the gate is already satisfied.
    graph2, _ = _build_graph([_call("c2", "write_file", {"path": "b.txt"})], _REMINDER_ON)
    out, _ = _invoke(graph2, "t-second-b", "sess-first-call")
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert any(_SENTINEL in m.content for m in tool_messages)


def test_rejection_blocks_the_call_and_is_not_remembered():
    graph, _ = _build_graph([_call("c1", "write_file", {"path": "notes.txt"})], _REMINDER_ON)
    config: RunnableConfig = {"configurable": {"thread_id": "t-reject"}}
    _invoke(graph, "t-reject", "sess-first-call")
    assert len(_pending_tasks(graph, config)) == 1

    out = _resume(graph, config, "reject", "no thanks")
    errors = [m for m in out["messages"] if isinstance(m, ToolMessage) and m.status == "error"]
    assert errors, "a rejected first call must not execute the tool"
    assert "no thanks" in errors[0].content
    assert BLOCKED_MESSAGE in errors[0].content
    assert _confirmed("sess-first-call") == []


def test_yolo_mode_bypasses_the_gate():
    graph, _ = _build_graph(
        [_call("c1", "write_file", {"path": "notes.txt"})],
        HITLConfig(yolo_mode=True, first_call_confirmation_enabled=True),
    )
    out, _ = _invoke(graph, "t-yolo", "sess-first-call")
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert any(_SENTINEL in m.content for m in tool_messages)


def test_unlisted_tools_are_untouched():
    graph, _ = _build_graph([_call("c1", "read_file", {"path": "notes.txt"})], _REMINDER_ON)
    out, _ = _invoke(graph, "t-read", "sess-first-call")
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert any("read:notes.txt" in m.content for m in tool_messages)


def test_no_operator_scope_skips_the_gate():
    """Cron / heartbeat / subagent-carrier turns have nobody to answer.

    The gate must not suspend such a turn; those scopes stay governed by their
    own policy (so the tool call proceeds here).
    """
    from agent.middlewares.humanInTheLoop.approval_scope import operator_scope

    graph, _ = _build_graph([_call("c1", "write_file", {"path": "notes.txt"})], _REMINDER_ON)
    config: RunnableConfig = {"configurable": {"thread_id": "t-no-operator"}}
    with operator_scope(None):
        out = graph.invoke(
            {
                "messages": [HumanMessage(content="system injection")],
                "session_id": "sess-first-call",
            },
            config,
        )
    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert any(_SENTINEL in m.content for m in tool_messages)
