"""The missing-result placeholder is a repair marker, not a tool error."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pub.func.transcript_repair import (
    EMPTY_TOOL_RESULT,
    MISSING_TOOL_RESULT,
    sanitize_tool_use_result_pairing,
)

pytestmark = [pytest.mark.unit]


def test_a_missing_result_is_a_neutral_marker_not_an_error():
    messages = [
        HumanMessage(content="go"),
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"file_path": "a"}, "id": "call-1"}],
        ),
    ]

    repaired = sanitize_tool_use_result_pairing(messages)

    placeholders = [
        message
        for message in repaired
        if isinstance(message, ToolMessage) and message.tool_call_id == "call-1"
    ]
    assert len(placeholders) == 1
    placeholder = placeholders[0]
    assert placeholder.status != "error"
    assert placeholder.additional_kwargs["sherry_repair"] == "missing_tool_result"
    assert placeholder.content == MISSING_TOOL_RESULT


def test_a_genuinely_missing_result_uses_the_accurate_marker():
    messages = [
        HumanMessage(content="go"),
        AIMessage(
            content="",
            tool_calls=[{"name": "terminal", "args": {}, "id": "call-4"}],
        ),
    ]

    repaired = sanitize_tool_use_result_pairing(messages)

    placeholder = next(message for message in repaired if isinstance(message, ToolMessage))
    assert placeholder.content == MISSING_TOOL_RESULT
    assert placeholder.status != "error"
    assert placeholder.additional_kwargs["sherry_repair"] == "missing_tool_result"
    assert "context trim" not in placeholder.content


def test_a_real_tool_error_keeps_its_error_status():
    messages = [
        HumanMessage(content="go"),
        AIMessage(
            content="",
            tool_calls=[{"name": "terminal", "args": {}, "id": "call-2"}],
        ),
        ToolMessage(content="boom", tool_call_id="call-2", status="error"),
    ]

    repaired = sanitize_tool_use_result_pairing(messages)

    tool_messages = [message for message in repaired if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"


def test_an_empty_result_is_kept_with_a_default_hint():
    """A successful tool with no output must not become a missing placeholder."""
    messages = [
        HumanMessage(content="go"),
        AIMessage(
            content="",
            tool_calls=[{"name": "terminal", "args": {}, "id": "call-3"}],
        ),
        ToolMessage(content="", tool_call_id="call-3", name="terminal", status="success"),
    ]

    repaired = sanitize_tool_use_result_pairing(messages)

    tool_messages = [message for message in repaired if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == EMPTY_TOOL_RESULT
    assert tool_messages[0].status != "error"
    assert tool_messages[0].content == EMPTY_TOOL_RESULT
    assert tool_messages[0].additional_kwargs["sherry_repair"] == "empty_result"
    assert "missing" not in str(tool_messages[0].content).lower()
