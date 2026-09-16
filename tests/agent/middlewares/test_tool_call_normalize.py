"""P0-1: ToolCallNormalize must not rewrite a healthy transcript.

The hook previously emitted ``RemoveMessage(REMOVE_ALL_MESSAGES)`` + a full
message rebuild on EVERY ``before_model`` call, even when
``sanitize_tool_use_result_pairing`` repaired nothing. The fast path returns
``None`` (no state write) when the sanitized list is element-identical to the
input; any real repair (synthesized/dropped ToolMessage, stale RemoveMessage
residue) still rebuilds.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from agent.middlewares.tool_call_normalize import ToolCallNormalize

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _healthy_history() -> list:
    return [
        HumanMessage(content="hello", id="h1"),
        AIMessage(content="hi", id="a1"),
        HumanMessage(content="run it", id="h2"),
        AIMessage(
            content="",
            id="a2",
            tool_calls=[{"name": "echo", "args": {"x": 1}, "id": "c1", "type": "tool_call"}],
        ),
        ToolMessage(content="ok", tool_call_id="c1", id="t1"),
    ]


class TestToolCallNormalizeFastPath:
    def test_healthy_transcript_returns_none(self):
        # Given a transcript whose tool pairing is already valid
        state = {"messages": _healthy_history()}

        # When the before_model implementation runs
        result = ToolCallNormalize()._before_model_impl(state)

        # Then it produces no state update at all (no RemoveMessage rebuild)
        assert result is None

    def test_healthy_transcript_is_not_mutated(self):
        messages = _healthy_history()
        before = list(messages)

        ToolCallNormalize()._before_model_impl({"messages": messages})

        # The fast path must not replace or reorder the message objects.
        assert messages == before
        assert all(a is b for a, b in zip(messages, before))

    def test_dangling_tool_call_rebuilds_with_placeholder(self):
        # Given an AIMessage tool_call with no following ToolMessage
        messages = [
            HumanMessage(content="q", id="h1"),
            AIMessage(
                content="",
                id="a1",
                tool_calls=[{"name": "echo", "args": {}, "id": "c1", "type": "tool_call"}],
            ),
        ]

        # When the hook runs
        result = ToolCallNormalize()._before_model_impl({"messages": messages})

        # Then the full rebuild still happens with the synthesized placeholder
        assert result is not None
        emitted = result["messages"]
        assert isinstance(emitted[0], RemoveMessage)
        placeholders = [m for m in emitted if isinstance(m, ToolMessage)]
        assert len(placeholders) == 1
        assert placeholders[0].tool_call_id == "c1"
        assert placeholders[0].status == "error"
        assert "missing" in placeholders[0].content

    def test_stale_remove_message_residue_still_rebuilds(self):
        # Given a RemoveMessage left in state by an earlier update
        human = HumanMessage(content="q", id="h1")
        messages = [human, RemoveMessage(id="stale")]

        # When the hook runs
        result = ToolCallNormalize()._before_model_impl({"messages": messages})

        # Then the residue is filtered and committed through a rebuild
        assert result is not None
        emitted = result["messages"]
        assert isinstance(emitted[0], RemoveMessage)
        assert len(emitted) == 2
        assert emitted[1] is human

    def test_invalid_tool_calls_repair_rebuilds(self):
        # Given an AIMessage carrying invalid_tool_calls (sanitizer clears them
        # via model_copy -> a different object with different content)
        messages = [
            HumanMessage(content="q", id="h1"),
            AIMessage(
                content="x",
                id="a1",
                invalid_tool_calls=[
                    {"name": "echo", "args": "{}", "id": "c9", "error": "bad parse"}
                ],
            ),
        ]

        result = ToolCallNormalize()._before_model_impl({"messages": messages})

        assert result is not None
        repaired = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert repaired[0].invalid_tool_calls == []
