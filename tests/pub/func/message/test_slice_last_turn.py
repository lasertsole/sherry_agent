"""Unit tests for slice_last_turn / slice_last_n_turn.

Behavior locked: a turn starts at the FIRST HumanMessage of a run of
consecutive HumanMessages. Batch-drained turns deliver N HumanMessages in a
single agent turn, and ALL of them must survive the slice so persistence
keeps the whole batch.

Real langchain_core messages are used (the implementation dispatches on
isinstance checks). ASCII only, no network.
"""

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from pub.func.message.slice_last_turn import slice_last_n_turn, slice_last_turn

pytestmark = [pytest.mark.unit]


def _human(text: str) -> HumanMessage:
    return HumanMessage(content=text)


def _ai(text: str) -> AIMessage:
    return AIMessage(content=text)


def _tool(text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id="tc-1")


class TestSliceLastTurnBatch:
    def test_batch_human_messages_are_preserved(self):
        """[Human(A), Human(B), AI] -> all three kept (batch drain)."""
        msgs: list[BaseMessage] = [_human("A"), _human("B"), _ai("reply")]

        result = slice_last_turn(msgs)

        assert result["messages"] == msgs
        assert result["dropped"] == 0

    def test_batch_after_previous_turn_keeps_only_batch(self):
        """[Human(X), AI, Human(A), Human(B), AI] -> [A, B, AI]."""
        msgs: list[BaseMessage] = [
            _human("X"),
            _ai("old reply"),
            _human("A"),
            _human("B"),
            _ai("reply"),
        ]

        result = slice_last_turn(msgs)

        assert result["messages"] == msgs[2:]
        assert result["dropped"] == 2

    def test_slice_last_n_turn_two_keeps_both_human_blocks(self):
        """n=2 with two human-blocks keeps both blocks."""
        msgs: list[BaseMessage] = [
            _human("X"),
            _ai("old reply"),
            _human("A"),
            _human("B"),
            _ai("reply"),
        ]

        result = slice_last_n_turn(msgs, 2)

        assert result["messages"] == msgs
        assert result["dropped"] == 0

    def test_three_consecutive_humans_kept_as_one_turn(self):
        """A 3-message batch is a single turn boundary, not three."""
        msgs: list[BaseMessage] = [
            _ai("prev"),
            _human("A"),
            _human("B"),
            _human("C"),
            _ai("reply"),
        ]

        result = slice_last_turn(msgs)

        assert result["messages"] == msgs[1:]
        assert result["dropped"] == 1


class TestSliceLastTurnNormal:
    def test_single_human_turn_unchanged(self):
        """[AI, Human(U), AI, Tool] -> unchanged from current behavior."""
        msgs: list[BaseMessage] = [
            _ai("prev"),
            _human("U"),
            _ai("reply"),
            _tool("result"),
        ]

        result = slice_last_turn(msgs)

        assert result["messages"] == msgs[1:]
        assert result["dropped"] == 1

    def test_no_human_message_keeps_everything(self):
        """Existing fallback: no HumanMessage -> keep all, drop nothing."""
        msgs: list[BaseMessage] = [_ai("a"), _tool("t")]

        result = slice_last_turn(msgs)

        assert result["messages"] == msgs
        assert result["dropped"] == 0

    def test_empty_list(self):
        """Existing fallback: empty input -> empty output."""
        result = slice_last_turn([])

        assert result["messages"] == []
        assert result["tokens"] == 0
        assert result["dropped"] == 0

    def test_tokens_reported_for_kept_slice(self):
        """Returned shape is {messages, tokens, dropped} with int tokens."""
        msgs: list[BaseMessage] = [_human("A"), _human("B"), _ai("reply")]

        result = slice_last_turn(msgs)

        assert isinstance(result["tokens"], int)
        assert result["tokens"] >= 0
        assert set(result) == {"messages", "tokens", "dropped"}
