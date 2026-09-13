"""E2E: question tool suspend/resume timeline through HeartbeatStaleness.

Verifies the interplay contract while the graph is suspended on the question
tool's interrupt():
- wrap_tool_call set heartbeat_skip and did NOT set heartbeat_tool
- heartbeat checks skip stale counting during the suspension
- resume returns the user's answer and clears the skip flag
"""

from typing import Any, ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.middlewares.heartbeat_staleness import (
    _STATE_KEY_KILLED,
    _STATE_KEY_SKIP,
    _STATE_KEY_STALE,
    _STATE_KEY_TOOL,
    HeartbeatStaleness,
)
from agent.tools.question import build_question_tool
from runtime import state_register_mem, timer_call_register

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

_HEARTBEAT_TIMER = "heartbeat_staleness_check"


class _ScriptedModel(BaseChatModel):
    """Emits one scripted question tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-question-e2e"

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


def _build_graph(scripted_calls):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)
    return create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[build_question_tool()],
        middleware=[HeartbeatStaleness()],
    )


@pytest.fixture
def clean_state(monkeypatch):
    """Reset state_register_mem to empty; drop the heartbeat timer on teardown."""
    monkeypatch.setattr(state_register_mem, "_states", {})
    yield
    for session_id in list(state_register_mem._states.keys()):
        timer_call_register.unregister(session_id, _HEARTBEAT_TIMER)


def _tool_messages(out) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


QUESTION_CALL = {
    "name": "question",
    "args": {
        "question": "Which database should we use?",
        "header": "DB choice",
        "options": [
            {"label": "Postgres", "description": "Full-featured relational DB"},
            {"label": "SQLite", "description": "Embedded single-file DB"},
        ],
        "multiple": False,
    },
    "id": "c1",
}


class TestQuestionTimeline:
    def test_suspend_resume_approve_returns_user_answer(self, clean_state):
        graph = _build_graph([QUESTION_CALL])
        config = {"configurable": {"thread_id": "t-q-approve"}}
        session_id = "s-q-approve"

        out = graph.invoke(
            {"messages": [HumanMessage(content="choose one")], "session_id": session_id},
            config,
        )

        assert "__interrupt__" in out
        pending = out["__interrupt__"][0].value
        assert pending["action_requests"][0]["name"] == "question"
        assert pending["action_requests"][0]["args"]["question"] == (
            "Which database should we use?"
        )
        assert state_register_mem.get_state(session_id, _STATE_KEY_SKIP, False) is True
        assert state_register_mem.get_state(session_id, _STATE_KEY_TOOL, "unset") is None
        assert state_register_mem.get_state(session_id, _STATE_KEY_KILLED, False) is False

        # Heartbeat ticks during the suspension must not count toward staleness.
        suspended_mw = HeartbeatStaleness(stale_cycles_idle=2, stale_cycles_in_tool=3)
        stale_before = state_register_mem.get_state(session_id, _STATE_KEY_STALE, 0)
        for _ in range(5):
            suspended_mw._check_progress(session_id)
        assert state_register_mem.get_state(session_id, _STATE_KEY_STALE, 0) == stale_before

        out = graph.invoke(
            Command(resume={"decisions": [{"type": "approve", "message": "Use Postgres please"}]}),
            config,
        )

        tool_msgs = _tool_messages(out)
        assert len(tool_msgs) == 1
        assert 'User answered: "Use Postgres please". You can now continue.' == (
            tool_msgs[0].content
        )
        assert out["messages"][-1].content == "Done."
        assert state_register_mem.get_state(session_id, _STATE_KEY_SKIP, False) is False
        assert state_register_mem.get_state(session_id, _STATE_KEY_TOOL, "unset") is None

    def test_suspend_resume_reject_returns_decline(self, clean_state):
        graph = _build_graph([QUESTION_CALL])
        config = {"configurable": {"thread_id": "t-q-reject"}}
        session_id = "s-q-reject"

        graph.invoke(
            {"messages": [HumanMessage(content="choose one")], "session_id": session_id},
            config,
        )
        out = graph.invoke(Command(resume={"decisions": [{"type": "reject"}]}), config)

        tool_msgs = _tool_messages(out)
        assert tool_msgs[0].content == (
            "User declined to answer. Proceed without this information."
        )
        assert state_register_mem.get_state(session_id, _STATE_KEY_SKIP, False) is False
