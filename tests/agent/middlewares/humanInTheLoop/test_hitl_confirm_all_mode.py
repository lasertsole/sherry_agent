"""Tests for the strict "confirm before changes" access mode.

The mode is the third position of the toolbar's access-mode control, next to
``auto_edit`` (default) and ``full_access`` (bypass-all). Its contract:

* every terminal command escalates to human approval — even a harmless one —
  and so does every call of a file-mutation tool;
* the earlier "don't ask again" grants (session / permanent allowlists, the
  remembered first-call confirmation) do NOT silence it: the mode is an explicit
  "ask me first" instruction;
* the hardline blocklist and the user deny rules still block outright (they sit
  earlier in the pipeline);
* YOLO wins if both ends of the setting ever read as set, so a bypass-all session
  is never asked;
* switching the mode clears the strict flag again.

Both halves are covered: the pipeline on its own (fast, no graph) and the
first-call gate on a REAL ``create_agent`` graph driven by a scripted model.
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

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.middlewares.humanInTheLoop.approval import (
    ApprovalPipeline,
    is_confirm_all_mode,
    set_session_confirm_all,
    set_session_yolo,
)
from agent.middlewares.humanInTheLoop.types import (
    HITL_SESSION_APPROVED_KEY,
    SESSION_CONFIRM_ALL_KEY,
    SESSION_YOLO_KEY,
)
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.module]

_SESSION = "sess-confirm-all"
_SENTINEL = "__FILE_WRITTEN__"


@pytest.fixture(autouse=True)
def _clean_flags():
    """Every test starts (and ends) with a session that has no flag set."""
    for key in (SESSION_CONFIRM_ALL_KEY, SESSION_YOLO_KEY, HITL_SESSION_APPROVED_KEY):
        state_register_mem.set_state(
            _SESSION, key, [] if key == HITL_SESSION_APPROVED_KEY else False
        )
    yield
    for key in (SESSION_CONFIRM_ALL_KEY, SESSION_YOLO_KEY, HITL_SESSION_APPROVED_KEY):
        state_register_mem.set_state(
            _SESSION, key, [] if key == HITL_SESSION_APPROVED_KEY else False
        )


# ── Pipeline level ──────────────────────────────────────────────────────


def _pipeline(config: HITLConfig | None = None) -> ApprovalPipeline:
    return ApprovalPipeline(config or HITLConfig(), lambda *_: None)


def test_ordinary_commands_escalate_in_strict_mode():
    pipeline = _pipeline()
    # Baseline: this one is approved silently in the default mode.
    assert pipeline.check_command("ls -la", _SESSION).approved is True

    set_session_confirm_all(_SESSION)

    result = pipeline.check_command("ls -la", _SESSION)
    assert result.approved is False
    assert result.decision is None, "strict mode escalates, it does not deny"
    assert "Confirm-all mode" in result.reason


def test_dangerous_commands_escalate_with_their_tags_in_strict_mode():
    set_session_confirm_all(_SESSION)

    result = _pipeline().check_command("rm -rf ./build", _SESSION)

    assert result.approved is False and result.decision is None
    assert "rm_recursive_force" in result.reason


def test_hardline_and_deny_rules_still_block_in_strict_mode():
    set_session_confirm_all(_SESSION)
    pipeline = _pipeline(HITLConfig(deny_rules=["docker rm*"]))

    hardline = pipeline.check_command("rm -rf /", _SESSION)
    deny_rule = pipeline.check_command("docker rm -f web", _SESSION)

    assert hardline.approved is False and hardline.decision is not None
    assert deny_rule.approved is False and deny_rule.decision is not None


def test_earlier_allowlist_grants_do_not_silence_strict_mode():
    """The mode outranks a "don't ask again" grant made under a laxer mode."""
    state_register_mem.set_state(_SESSION, HITL_SESSION_APPROVED_KEY, ["ls*"])
    set_session_confirm_all(_SESSION)

    result = _pipeline().check_command("ls -la", _SESSION)

    assert result.approved is False and result.decision is None


def test_yolo_wins_over_the_strict_flag():
    set_session_confirm_all(_SESSION)
    set_session_yolo(_SESSION)

    assert is_confirm_all_mode(HITLConfig(), _SESSION) is False
    result = _pipeline().check_command("ls -la", _SESSION)
    assert result.approved is True
    assert "YOLO" in result.reason


def test_the_two_flags_clear_each_other():
    set_session_confirm_all(_SESSION)
    assert state_register_mem.get_state(_SESSION, SESSION_CONFIRM_ALL_KEY, False) is True
    assert state_register_mem.get_state(_SESSION, SESSION_YOLO_KEY, False) is False

    set_session_yolo(_SESSION)
    assert state_register_mem.get_state(_SESSION, SESSION_CONFIRM_ALL_KEY, False) is False
    assert is_confirm_all_mode(HITLConfig(), _SESSION) is False

    set_session_confirm_all(_SESSION)
    assert state_register_mem.get_state(_SESSION, SESSION_YOLO_KEY, False) is False
    assert is_confirm_all_mode(HITLConfig(), _SESSION) is True


def test_strict_mode_is_off_by_default():
    assert is_confirm_all_mode(HITLConfig(), _SESSION) is False
    assert is_confirm_all_mode(HITLConfig(), "") is False


# ── First-call gate on a real graph ─────────────────────────────────────


@tool("write_file")
def _write_file(path: str, content: str = "") -> str:
    """Fake file write (the gate only needs a listed, side-effecting tool)."""
    return f"{_SENTINEL}:{path}:{len(content)}"


class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-hitl-confirm-all"

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


def _build_graph(tool_calls: list[dict[str, Any]]):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(tool_calls)
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[_write_file],
        middleware=[HumanInTheLoop(HITLConfig())],
    )
    return graph


def _invoke(graph, thread_id: str) -> tuple[dict, RunnableConfig]:
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke(
        {"messages": [HumanMessage(content="please write the file")], "session_id": _SESSION},
        config,
    )
    return out, config


def _pending_tasks(graph, config: RunnableConfig):
    return list(graph.get_state(config).tasks or [])


def test_strict_mode_asks_on_every_file_change():
    """The remembered first-call confirmation is ignored while the mode is on."""
    call = {"id": "c1", "name": "write_file", "args": {"path": "a.txt"}, "type": "tool_call"}
    graph = _build_graph([call])
    config: RunnableConfig = {"configurable": {"thread_id": "t-strict-1"}}

    # First call: approve it (which would normally satisfy the gate for the session).
    _invoke(graph, "t-strict-1")
    assert len(_pending_tasks(graph, config)) == 1
    _resume = graph.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config,
    )
    assert any(_SENTINEL in m.content for m in _resume["messages"] if isinstance(m, ToolMessage))

    set_session_confirm_all(_SESSION)

    # Same session, same tool, second call: it must ask again.
    graph2 = _build_graph(
        [
            {
                "id": "c2",
                "name": "write_file",
                "args": {"path": "b.txt"},
                "type": "tool_call",
            }
        ]
    )
    out, config2 = _invoke(graph2, "t-strict-2")

    tasks = _pending_tasks(graph2, config2)
    assert len(tasks) == 1, f"expected a fresh prompt, got {len(tasks)}"
    description = tasks[0].interrupts[0].value["action_requests"][0]["description"]
    assert "Confirm-all mode" in description
    assert not [m for m in out["messages"] if isinstance(m, ToolMessage)], "nothing ran yet"

    # Approving in strict mode does not remember the tool either: the next call
    # still asks (the memory is only meaningful outside the mode).
    graph2.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config2)
    graph3 = _build_graph(
        [
            {
                "id": "c3",
                "name": "write_file",
                "args": {"path": "c.txt"},
                "type": "tool_call",
            }
        ]
    )
    _invoke(graph3, "t-strict-3")
    assert len(_pending_tasks(graph3, {"configurable": {"thread_id": "t-strict-3"}})) == 1


def test_yolo_bypasses_the_strict_gate():
    set_session_confirm_all(_SESSION)
    set_session_yolo(_SESSION)
    call = {"id": "c1", "name": "write_file", "args": {"path": "a.txt"}, "type": "tool_call"}
    graph = _build_graph([call])

    out, _ = _invoke(graph, "t-strict-yolo")

    assert any(_SENTINEL in m.content for m in out["messages"] if isinstance(m, ToolMessage))
