"""Microcompact: local clearing of old tool results (no LLM)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middlewares.summarization.microcompact import (
    CLEARED_MARKER,
    maybe_local_microcompact,
    resolve_microcompact_trigger,
)

pytestmark = [pytest.mark.unit]


def _tool(name: str, call_id: str, chars: int = 40_000) -> ToolMessage:
    return ToolMessage(content="x" * chars, name=name, tool_call_id=call_id)


def _conversation(count: int = 4, chars: int = 40_000) -> list:
    messages: list = [HumanMessage(content="start")]
    for index in range(count):
        messages.append(AIMessage(content=f"call {index}"))
        messages.append(_tool("terminal", f"call_{index}", chars))
    return messages


def _cleared(messages) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage) and message.content == CLEARED_MARKER
    ]


def test_token_pressure_clears_only_old_tool_results():
    messages = _conversation(count=6)
    updated, saved = maybe_local_microcompact(messages, auto_compact_threshold=20_000)

    assert saved > 0
    # The last 5 messages always survive; older tool results are cleared.
    assert all(index < len(messages) - 5 for index in _cleared(updated))
    assert updated[-1] is messages[-1]
    assert updated[-2] is messages[-2]
    # Pairing fields survive the in-place stub.
    first = next(message for message in updated if isinstance(message, ToolMessage))
    assert first.tool_call_id == "call_0"
    assert first.name == "terminal"


def test_below_the_threshold_is_a_no_op():
    messages = _conversation(count=1, chars=100)
    updated, saved = maybe_local_microcompact(messages, auto_compact_threshold=100_000)

    assert saved == 0
    assert updated is messages


def test_media_results_are_never_cleared():
    messages = [
        HumanMessage(content="start"),
        AIMessage(content="call"),
        ToolMessage(
            content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}],
            name="read_file",
            tool_call_id="img",
        ),
        AIMessage(content="call2"),
        ToolMessage(content="y" * 40_000, name="terminal", tool_call_id="txt"),
        AIMessage(content="tail1"),
        AIMessage(content="tail2"),
        AIMessage(content="tail3"),
        AIMessage(content="tail4"),
        AIMessage(content="tail5"),
    ]
    updated, saved = maybe_local_microcompact(messages, auto_compact_threshold=1_000)

    assert saved > 0
    assert updated[2] is messages[2]  # the media result is untouched
    assert updated[4].content == CLEARED_MARKER


def test_already_cleared_results_are_not_counted_again():
    messages = _conversation(count=2)
    once, _ = maybe_local_microcompact(messages, auto_compact_threshold=1_000)
    twice, saved = maybe_local_microcompact(once, auto_compact_threshold=1_000)
    assert saved == 0
    assert twice == once


def test_small_savings_are_discarded():
    messages = _conversation(count=2, chars=200)
    updated, saved = maybe_local_microcompact(messages, auto_compact_threshold=1_000)
    assert saved == 0
    assert updated is messages


def test_idle_trigger_ignores_token_pressure():
    messages = _conversation(count=2)
    assert (
        resolve_microcompact_trigger(
            messages,
            auto_compact_threshold=10**9,
            last_assistant_completed_at_ms=0,
            now_ms=61 * 60_000,
        )
        == "time_based"
    )
    assert (
        resolve_microcompact_trigger(
            messages,
            auto_compact_threshold=10**9,
            last_assistant_completed_at_ms=0,
            now_ms=59 * 60_000,
        )
        is None
    )


def test_disabled_config_is_a_no_op(monkeypatch: pytest.MonkeyPatch):
    from config.features import SUMMARIZATION

    monkeypatch.setitem(SUMMARIZATION, "microcompact_enabled", False)
    messages = _conversation(count=4)
    updated, saved = maybe_local_microcompact(messages, auto_compact_threshold=1_000)
    assert saved == 0
    assert updated is messages
