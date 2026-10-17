"""A child's checkpoint outlives its own connection — the premise behind
``sessions_messages`` (the subagent test suite stubs ``agent.checkpointer``,
so the real-connection proof lives here).

Shape under test: a child agent writes its transcript through its own
checkpointer connection and, at the end of the run,
``_release_child_resources`` only CLOSES that connection. The caller — the
main agent holding a different connection to the same
``SRC_DIR/checkpoints/sqlite.db`` — must still find the thread and read the
transcript back, which is what the ``sessions_messages`` tool does after a
timeout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.state_port import read_messages
from agent.tools.subagent.tools.sessions_messages import read_child_messages

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

_CHILD_KEY = "agent:main:subagent:timeout-probe"


async def _child_shaped_graph(checkpointer):
    """A two-node graph standing in for a child agent's compiled graph."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import MessagesState

    async def _respond(_state) -> dict:
        return {"messages": [AIMessage(content="step one done")]}

    builder = StateGraph(MessagesState)
    builder.add_node("respond", _respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)


@pytest.fixture()
def real_checkpoint_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real checkpointer + config builder, pointed at a per-test directory.

    ``tests/agent/tools/subagent/conftest.py`` replaces
    ``pub.func.build_agent_config`` (and the factory on the ``agent.checkpointer``
    package) with hermetic stand-ins for the WHOLE pytest process — the stand-in
    config is ``{}``, which carries no ``configurable.thread_id``. Reading a real
    transcript needs both back, so they are restored here for the test's
    duration; the concrete module paths are what makes the imports real in
    either regime.
    """
    import importlib

    import agent.checkpointer as checkpointer_package
    import agent.checkpointer.async_sqlite_checkpointer as checkpointer_module
    import pub.func as pub_func_package

    real_factory = checkpointer_module.build_async_sqlite_checkpointer
    real_config = importlib.import_module("pub.func.build_agent_config").build_agent_config

    monkeypatch.setattr(checkpointer_module, "SRC_DIR", tmp_path)
    monkeypatch.setattr(
        checkpointer_package, "build_async_sqlite_checkpointer", real_factory, raising=False
    )
    monkeypatch.setattr(pub_func_package, "build_agent_config", real_config, raising=False)
    return real_factory, real_config


@pytest.mark.asyncio
async def test_a_released_child_checkpoint_is_readable_over_a_new_connection(
    real_checkpoint_env,
):
    real_factory, build_config = real_checkpoint_env
    child_checkpointer = await real_factory()
    child_graph = await _child_shaped_graph(child_checkpointer)
    try:
        await child_graph.ainvoke(
            {"messages": [HumanMessage(content="do the thing")]},
            build_config(_CHILD_KEY),
        )
    finally:
        # Exactly what _release_child_resources does: close, never delete.
        await child_checkpointer.aclose()

    reader = await real_factory()
    try:
        reader_graph = await _child_shaped_graph(reader)

        messages = await read_messages(session_id=_CHILD_KEY, graph=reader_graph)
        assert [m.content for m in messages] == ["do the thing", "step one done"]

        tail = await read_child_messages(_CHILD_KEY, 1, graph=reader_graph)
        assert [m.content for m in tail] == ["step one done"]
    finally:
        await reader.aclose()


@pytest.mark.asyncio
async def test_an_unknown_session_reads_as_empty(real_checkpoint_env):
    real_factory, _ = real_checkpoint_env
    reader = await real_factory()
    try:
        reader_graph = await _child_shaped_graph(reader)

        messages = await read_child_messages("agent:main:subagent:never-ran", 5, graph=reader_graph)

        assert messages == []
    finally:
        await reader.aclose()
