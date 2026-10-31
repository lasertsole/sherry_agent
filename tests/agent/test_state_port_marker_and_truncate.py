"""Transcript surgery at the turn boundaries (integration, real graph).

Two operations the continuation and the message-edit features are built on,
exercised against a REAL ``create_agent`` graph with an ``InMemorySaver``:

* ``restore_interrupt_marker`` — rewrite the marker's content in place (the
  ``add_messages`` reducer's upsert-by-id), refusing when the marker is gone
  (never appending a duplicate tail);
* ``truncate_messages_after_turn`` — remove turn N and everything after it
  through ``RemoveMessage``, counting turns over NON-internal human messages
  (reminder blocks / notices belong to the turn that follows them, not to a
  turn of their own), and answering ``-1`` once the turn is compacted away.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field

from agent.state_port import restore_interrupt_marker, truncate_messages_after_turn

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60), pytest.mark.integration]

SESSION = "state-port-surgery"
# A literal per-thread config: `tests/agent/tools/subagent/conftest.py` replaces
# the process-wide `pub.func.build_agent_config` with a `{}` stub (a leaked
# module-level assignment), so a test under `tests/agent` must not resolve the
# session config through it.
CONFIG: dict[str, Any] = {"configurable": {"thread_id": SESSION}}


class _FakeModel(BaseChatModel):
    """Canned reply; records the prompts it saw."""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "state-port-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="reply"))])


class _State(AgentState):
    session_id: str


@pytest.fixture()
def graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    return create_agent(
        model=_FakeModel(),
        tools=[],
        middleware=[],
        state_schema=_State,
        checkpointer=InMemorySaver(),
    )


def _config() -> dict[str, Any]:
    """The config every port call receives (see CONFIG's note)."""
    return CONFIG


async def _seed_two_turns(graph: CompiledStateGraph[Any, Any, Any, Any]) -> None:
    config = _config()
    await graph.ainvoke({"messages": [HumanMessage("q1")], "session_id": SESSION}, config)
    await graph.ainvoke({"messages": [HumanMessage("q2")], "session_id": SESSION}, config)


async def _messages(graph: CompiledStateGraph[Any, Any, Any, Any]) -> list[BaseMessage]:
    snapshot = await graph.aget_state(_config())
    return list((snapshot.values or {}).get("messages", []))


async def _contents(graph: CompiledStateGraph[Any, Any, Any, Any]) -> list[str]:
    return [str(message.content) for message in await _messages(graph)]


class TestRestoreInterruptMarker:
    async def test_rewrites_the_marker_in_place(self, graph) -> None:
        config = _config()
        await _seed_two_turns(graph)
        marker_id = "interrupted-thread-2"
        await graph.aupdate_state(
            config,
            {
                "messages": [
                    AIMessage(
                        content="[interrupted] half",
                        id=marker_id,
                        metadata={"interrupted": True, "reason": "cancelled"},
                    )
                ]
            },
            as_node="model",
        )

        restored = await restore_interrupt_marker(SESSION, config, marker_id, "half", graph)

        assert restored is True
        messages = await _messages(graph)
        assert messages[-1].id == marker_id
        assert messages[-1].content == "half"
        assert sum(1 for message in messages if message.id == marker_id) == 1
        # The interruption metadata does not survive: the continuation owns the
        # tail now, it is no longer an interrupted-turn marker.
        assert not (getattr(messages[-1], "metadata", None) or {}).get("interrupted")

    async def test_is_idempotent(self, graph) -> None:
        config = _config()
        await _seed_two_turns(graph)
        marker_id = "interrupted-thread-2"
        await graph.aupdate_state(
            config,
            {"messages": [AIMessage(content="[interrupted] half", id=marker_id)]},
            as_node="model",
        )

        assert await restore_interrupt_marker(SESSION, config, marker_id, "half", graph) is True
        assert await restore_interrupt_marker(SESSION, config, marker_id, "half", graph) is True

        messages = await _messages(graph)
        assert [message.id for message in messages].count(marker_id) == 1

    async def test_refuses_when_the_marker_is_gone(self, graph) -> None:
        config = _config()
        await _seed_two_turns(graph)
        before = await _contents(graph)

        restored = await restore_interrupt_marker(SESSION, config, "missing-id", "half", graph)

        assert restored is False
        # Critically: nothing was APPENDED (an upsert of an unknown id would add
        # a phantom assistant message to the transcript).
        assert await _contents(graph) == before


class TestTruncateMessagesAfterTurn:
    async def test_removes_the_turn_and_everything_after_it(self, graph) -> None:
        await _seed_two_turns(graph)

        removed = await truncate_messages_after_turn(SESSION, 2, graph, _config())

        assert removed == 2
        assert await _contents(graph) == ["q1", "reply"]

    async def test_editing_the_first_turn_empties_the_transcript(self, graph) -> None:
        await _seed_two_turns(graph)

        removed = await truncate_messages_after_turn(SESSION, 1, graph, _config())

        assert removed == 4
        assert await _contents(graph) == []

    async def test_internal_human_messages_do_not_count_as_turns(self, graph) -> None:
        config = _config()
        await _seed_two_turns(graph)
        # A reminder / notice the framework injects: it belongs to the turn it
        # precedes, never a turn of its own.
        await graph.aupdate_state(
            config,
            {
                "messages": [
                    HumanMessage(
                        "notice", metadata={"internal": True, "origin": "project_dir_notice"}
                    )
                ]
            },
            as_node="model",
        )

        removed = await truncate_messages_after_turn(SESSION, 2, graph, _config())

        # The cut is at q2 (3 messages: q2, reply, notice), NOT at the notice —
        # counting the notice as turn 2 would remove only it.
        assert removed == 3
        assert await _contents(graph) == ["q1", "reply"]

    async def test_a_compacted_turn_answers_minus_one(self, graph) -> None:
        await _seed_two_turns(graph)

        assert await truncate_messages_after_turn(SESSION, 5, graph, _config()) == -1
        # Nothing was touched.
        assert await _contents(graph) == ["q1", "reply", "q2", "reply"]
