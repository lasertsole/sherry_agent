"""Regression: the batch-eviction guard resolves the SESSION, not the thread_id.

Measured on the DeepSWE L1 rerun of 2026-11-27: ``_session_id`` returned
``str(configurable["thread_id"])`` — the checkpointer's int hash of the session
id — because the product never passes ``context=``. A batch-aggregate offload
therefore wrote under ``SESSIONS_DIR/<hash>/evicted/``: outside the session
tree, uncovered by the session's own path exemption and never cleaned up.

The functional test drives a real graph so the runtime shape is the product's
(state carries ``session_id``; config carries only an int ``thread_id``).
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from agent.safe_tool_node import SafeToolNode, _session_id

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Context:
    """Attribute-shaped context (the user_hooks shape)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Runtime:
    def __init__(self, *, state: Any = None, context: Any = None, config: Any = None) -> None:
        self.state = state
        self.context = context
        self.config = config


def test_state_session_wins() -> None:
    runtime = _Runtime(state={"session_id": "s1"}, context={"session_id": "s2"})
    assert _session_id({"configurable": {"thread_id": 999}}, runtime) == "s1"


def test_context_dict_still_resolves() -> None:
    runtime = _Runtime(context={"session_id": "ctx-dict"})
    assert _session_id({}, runtime) == "ctx-dict"


def test_context_attribute_still_resolves() -> None:
    assert _session_id({}, _Runtime(context=_Context("ctx-attr"))) == "ctx-attr"


def test_thread_id_is_never_returned() -> None:
    # The old code returned "999" here and offloaded into SESSIONS_DIR/999/.
    assert _session_id({"configurable": {"thread_id": 999}}, _Runtime()) == ""


def test_configurable_session_id_is_the_last_resort() -> None:
    config = {"configurable": {"thread_id": 999, "session_id": "cfg-sid"}}
    assert _session_id(config, _Runtime()) == "cfg-sid"


def test_nothing_resolvable_is_empty() -> None:
    assert _session_id({}, _Runtime()) == ""


@tool
def todoread(seed: str = "y") -> str:  # noqa: ARG001 — the payload is what matters
    """Return a large single-line payload (read-only, so the batch runs concurrently)."""
    return seed * 60_000


@pytest.mark.asyncio
async def test_batch_offload_lands_in_the_session_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from agent import safe_tool_node
    from pub.func.message import eviction as eviction_mod

    monkeypatch.setattr(eviction_mod, "SESSIONS_DIR", tmp_path)
    monkeypatch.setitem(safe_tool_node.TOOL_PARALLEL, "batch_result_threshold_chars", 1_000)

    node = SafeToolNode([todoread])
    builder = StateGraph(dict)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "todoread", "args": {}, "id": "c1"},
            {"name": "todoread", "args": {}, "id": "c2"},
        ],
    )

    out = await graph.ainvoke(
        {"messages": [message], "session_id": "sess-1"},
        config={"configurable": {"thread_id": 999}},
    )

    messages = out["messages"][-2:]
    assert all("[evicted to: " in str(m.content) for m in messages)
    assert (tmp_path / "sess-1" / "evicted").is_dir()
    assert not (tmp_path / "999").exists()
