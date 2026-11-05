"""Semantic memory recall as an async reminder source."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.reminder_catalog import core
from agent.middlewares.reminder_catalog.sources import _memory_recall_builder


class _FakeStore:
    semantic_recall_enabled = True
    semantic_recall_top_k = 2
    semantic_recall_char_budget = 500
    repo = object()


@pytest.mark.asyncio
async def test_builder_renders_recalled_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.tools.memory.memory_store", _FakeStore())

    async def _recall(query, **kwargs):
        assert query == "how do I deploy?"
        assert kwargs["limit"] == 2
        return [
            {
                "id": 7,
                "target": "memory",
                "content": "deploy uses the staging script",
                "entry_type": "project",
                "score": 0.8123,
                "usage_count": 3,
                "stale": False,
            }
        ]

    monkeypatch.setattr("context_engine.store.memory_recall.semantic_recall", _recall)
    state = {"messages": [HumanMessage(content="how do I deploy?")]}
    text = await _memory_recall_builder("s1", state)
    assert text is not None
    assert "deploy uses the staging script" in text
    assert "memory" in text
    assert "used 3x" in text


@pytest.mark.asyncio
async def test_builder_is_off_when_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Off(_FakeStore):
        semantic_recall_enabled = False

    monkeypatch.setattr("agent.tools.memory.memory_store", _Off())
    assert await _memory_recall_builder("s1", {"messages": [HumanMessage(content="x")]}) is None


@pytest.mark.asyncio
async def test_async_builder_is_awaited_by_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _builder(_session_id, _state):
        return "recall block"

    source = core.ReminderSource(
        source_id="async_test",
        channel=core.ReminderChannel.CURRENT_TURN,
        builder=_builder,
        budget_chars=100,
        priority=99,
    )
    monkeypatch.setattr(core, "_SOURCES", {"async_test": source})
    monkeypatch.setitem(core.REMINDER_CATALOG, "enabled", True)
    core.clear_session("s-async")
    result = await core.aresolve_injections("s-async", {}, current_turn=1)
    assert result == {"async_test": "recall block"}
