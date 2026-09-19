"""Reducer/channel contract for the agent state ``messages`` key (P1-4 / P1-5).

Evaluation verdict these tests lock in (see ``TODO/DEEPAGENTS_BORROWING_PLAN.md``):

P1-5 — *covered by LangGraph's standard ``add_messages``*, so no custom reducer
is added. The plan's sample reducer skips a same-id write; the standard reducer
*replaces* it, and P1-9 (in-place tagging via ``model_copy``) depends on that
replacement. A skip-on-duplicate reducer would silently drop the tag.

P1-4 — DeltaChannel is available in the installed langgraph and the project's
saver implements ``get_delta_channel_history``, but Sherry's
``aclean_old_checkpoints`` keeps only the newest checkpoint per thread. That
pruning severs DeltaChannel's ancestor-write replay and makes delta state
reconstruct as **empty** (LangGraph documents this hazard). The tests below
pin the current safe choice: the ``messages`` channel stays the standard
snapshotting ``add_messages`` reducer, not a ``DeltaChannel``.
"""

from __future__ import annotations

import typing

import pytest
from langchain.agents.middleware import AgentState
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
)
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from agent.core import StateSchema

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _messages_metadata(schema: type) -> tuple[object, ...]:
    """Return the ``Annotated`` metadata attached to a schema's ``messages``."""
    hint = typing.get_type_hints(schema, include_extras=True)["messages"]
    if typing.get_origin(hint) in (typing.Required, typing.NotRequired):
        hint = typing.get_args(hint)[0]
    return tuple(typing.get_args(hint)[1:])


class TestStandardAddMessagesContract:
    """The standard reducer already provides every P1-5 feature."""

    def test_same_id_replaces_in_place(self):
        # Given two messages that share an id but differ in content
        first = HumanMessage(content="v1", id="h1")
        second = HumanMessage(content="v2", id="h1")

        # When merged through the standard reducer
        merged = add_messages([first], [second])

        # Then the newer message replaces the old one (no duplicate)
        assert len(merged) == 1
        assert merged[0].content == "v2"
        assert merged[0].id == "h1"

    def test_distinct_ids_append(self):
        merged = add_messages(
            [HumanMessage(content="a", id="h1")],
            [AIMessage(content="b", id="a1")],
        )
        assert [m.id for m in merged] == ["h1", "a1"]

    def test_remove_message_deletes_by_id(self):
        merged = add_messages(
            [HumanMessage(content="a", id="h1"), AIMessage(content="b", id="a1")],
            [RemoveMessage(id="h1")],
        )
        assert [m.id for m in merged] == ["a1"]

    def test_remove_unknown_id_raises(self):
        # The standard reducer refuses to tombstone a non-existent id rather
        # than silently ignoring it.
        with pytest.raises(ValueError):
            add_messages([HumanMessage(content="a", id="h1")], [RemoveMessage(id="nope")])

    def test_remove_all_messages_resets_to_following_messages(self):
        merged = add_messages(
            [HumanMessage(content="a", id="h1"), AIMessage(content="b", id="a1")],
            [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(content="fresh", id="h2"),
            ],
        )
        assert [m.id for m in merged] == ["h2"]

    def test_missing_ids_are_assigned(self):
        loud = HumanMessage(content="no-id")
        quiet = AIMessage(content="also-no-id")
        assert loud.id is None

        merged = add_messages([], [loud, quiet])

        assert all(m.id is not None for m in merged)
        assert merged[0].id != merged[1].id


class TestP19InPlaceTagging:
    """P1-9 relies on same-id replacement, not on a custom reducer."""

    def test_model_copy_tag_replaces_without_changing_id_or_count(self):
        # Given an oversized human message in state
        original: AnyMessage = HumanMessage(content="long payload", id="h1")

        # When P1-9 tags it in place (content unchanged, id unchanged)
        tagged = original.model_copy(
            update={"additional_kwargs": {"lc_evicted_to": "/tmp/evicted/h1.md"}}
        )

        # Then the reducer replaces the entry instead of appending a copy
        merged = add_messages([original], [tagged])
        assert len(merged) == 1
        assert merged[0].id == "h1"
        assert merged[0].content == "long payload"
        assert merged[0].additional_kwargs == {"lc_evicted_to": "/tmp/evicted/h1.md"}


class TestFullListReplacement:
    """ToolCallNormalize / Summarization rewrite the list via the sentinel."""

    def test_sentinel_plus_rebuilt_list_replaces_entire_history(self):
        # Given a history with an orphaned tool pairing
        stale = [
            HumanMessage(content="hi", id="h1"),
            AIMessage(content="", id="a1"),
        ]

        # When the repair path emits sentinel + rebuilt list
        repaired = [HumanMessage(content="hi", id="h1")]
        merged = add_messages(stale, [RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired])

        # Then the state is exactly the rebuilt list, in order
        assert [(m.id, m.content) for m in merged] == [("h1", "hi")]


class TestStateSchemaChannelChoice:
    """Guard the P1-4 decision: messages stays the standard snapshot channel."""

    def test_state_schema_does_not_redeclare_messages(self):
        assert StateSchema.__annotations__["messages"] is AgentState.__annotations__["messages"]

    def test_messages_reducer_is_standard_add_messages_not_delta(self):
        metadata = _messages_metadata(StateSchema)
        assert any(m is add_messages for m in metadata)
        assert not any(isinstance(m, DeltaChannel) for m in metadata)

    def test_agent_state_still_carries_add_messages(self):
        # Sanity-check the base contract this guard is anchored to.
        metadata = _messages_metadata(AgentState)
        assert any(m is add_messages for m in metadata)
