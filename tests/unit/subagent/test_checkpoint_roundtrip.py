"""Locking (characterization) test: carrier metadata survives a checkpointer roundtrip.

Wave-2 safety net for plan subagent-origin-tagging (Task 2). GREEN on the
untouched codebase — locks CURRENT behavior. The project checkpointer test
setup is too heavy for this suite (agent.checkpointer is stubbed by
conftest.py), so per the task spec this uses the fallback: a minimal langgraph
InMemorySaver roundtrip. InMemorySaver.put serializes channel values via
``serde.dumps_typed`` and get_state deserializes via ``loads_typed`` — a real
serialize → deserialize cycle, not a reference hand-back.
"""

from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.tools.subagent.announce.completion_message import build_completion_message
from agent.tools.subagent.types.registry import SubagentRunRecord

pytestmark = pytest.mark.unit


class _State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _make_run(run_id: str = "run-ck-1") -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key="agent:main:subagent:uuid-1",
        requester_session_key="sess-1",
        task="do research",
        label="test-child",
        task_name="research",
    )


def test_checkpoint_roundtrip_preserves_metadata():
    """Carrier HumanMessage metadata survives checkpointer put → get_state.

    Locks the load-bearing Wave-2 assumption that persisting/restoring a
    parent session's transcript through a langgraph checkpointer keeps the
    frozen metadata contract {internal, provenance, run_id, status} intact.
    """
    graph = StateGraph(_State)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    app = graph.compile(checkpointer=InMemorySaver())

    carrier = build_completion_message(_make_run(), "child finished", "completed")
    config: RunnableConfig = {"configurable": {"thread_id": "t-carrier"}}
    _ = app.invoke({"messages": [carrier]}, config)

    state = app.get_state(config)
    messages = state.values.get("messages") or []
    assert messages, "checkpointer state must contain the input messages"

    last = messages[-1]
    assert isinstance(last, HumanMessage)
    meta = getattr(last, "metadata", None) or {}
    assert set(meta.keys()) == {"internal", "provenance", "run_id", "status"}
    assert meta == {
        "internal": True,
        "provenance": "subagent_completion",
        "run_id": "run-ck-1",
        "status": "completed",
    }
