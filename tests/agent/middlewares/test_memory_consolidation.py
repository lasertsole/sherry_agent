"""Consolidation cost gate, prompt rendering and the manual task wrapper."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.summarization import nudges
from agent.middlewares.summarization.nudges import (
    _render_memory_entries_for_consolidation,
    run_memory_consolidation,
)
from agent.tools.memory import MemoryStore
from context_engine.store.memory_entry_repository import MemoryEntryRepository


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    repo = MemoryEntryRepository(tmp_path / "memory.db")
    store = MemoryStore(repo=repo)
    monkeypatch.setattr("agent.tools.memory.memory_store", store)
    yield store
    repo.close()


def test_below_threshold_only_ages(store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(nudges.MEMORY_TOOL, "consolidation_min_entries", 10)
    store.repo.add_entry("memory", "one")
    store.repo.add_entry("user", "two")

    result = asyncio.run(run_memory_consolidation("s1", system_prompt="sys"))
    assert result["status"] == "skipped"
    assert result["reason"] == "too_few_entries"
    assert result["entries"] == 2


def test_listing_contains_ids_usage_and_targets(store: MemoryStore) -> None:
    store.repo.add_entry("memory", "alpha note")
    store.repo.add_entry("facts", "beta pitfall")
    listing = _render_memory_entries_for_consolidation()
    assert "alpha note" in listing
    assert "beta pitfall" in listing
    assert "### memory" in listing
    assert "### facts" in listing
    assert "id " in listing


def test_llm_pass_runs_when_due(store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(nudges.MEMORY_TOOL, "consolidation_min_entries", 1)
    store.repo.add_entry("memory", "duplicate A")
    captured: dict[str, object] = {}

    monkeypatch.setattr(nudges, "_build_session_model", lambda _sid: object())
    monkeypatch.setattr(nudges, "_build_memory_review_agent", lambda *a, **k: object())

    async def _invoke(agent, session_id, messages, prompt, **kwargs):
        captured["prompt"] = prompt
        captured["session"] = session_id
        return HumanMessage(content="Nothing to consolidate.")

    monkeypatch.setattr(nudges, "_invoke_memory_review", _invoke)

    result = asyncio.run(run_memory_consolidation("s1", system_prompt="system prompt", timeout_s=5))
    assert result["status"] == "completed"
    assert result["entries_before"] == 1
    assert "duplicate A" in str(captured["prompt"])
    assert captured["session"] == "s1"
    assert "Nothing to consolidate" in result["reply"]
