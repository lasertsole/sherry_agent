"""Memory provenance, the ``read`` action and usage-aware eviction."""

from __future__ import annotations

import json

import pytest

import agent.tools.memory as memory_module
from agent.tools.memory import MemoryStore, memory_tool
from context_engine.store.memory_entry_repository import MemoryEntryRepository

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def memory_repo(tmp_path, monkeypatch):
    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "memory_entry_repo", repo)
    yield repo
    repo.close()


def test_add_stamps_the_origin_session(memory_repo):
    store = MemoryStore()

    store.add("memory", "Environment: Python 3.13", origin_session="sess-origin")

    entry = memory_repo.active_entries_ordered("memory")[0]
    assert entry["origin_session"] == "sess-origin"


def test_system_prompt_renders_provenance_only_when_origin_exists(memory_repo):
    store = MemoryStore()
    store.add("memory", "with origin", origin_session="sess-123")
    memory_repo.add_entry("memory", "legacy entry", origin_session=None)
    store.load_from_store()

    block = store.format_for_system_prompt("memory") or ""

    assert "[origin: sess-123, created: " in block
    assert "used: 0x]" in block
    assert "legacy entry" in block
    # A legacy row has no origin marker at all.
    assert block.count("[origin:") == 1


def test_read_returns_entries_and_increments_usage(memory_repo):
    store = MemoryStore()
    store.add("memory", "alpha note")
    store.add("memory", "beta note")

    first = store.read("memory")
    second = store.read("memory")

    assert [entry["content"] for entry in first] == ["alpha note", "beta note"]
    assert [entry["usage_count"] for entry in first] == [1, 1]
    assert [entry["usage_count"] for entry in second] == [2, 2]


def test_read_with_query_filters_and_only_counts_matches(memory_repo):
    store = MemoryStore()
    store.add("memory", "alpha note")
    store.add("memory", "beta note")

    matches = store.read("memory", "ALPHA")

    assert [entry["content"] for entry in matches] == ["alpha note"]
    rows = {entry["content"]: entry for entry in memory_repo.list_entries("memory", limit=None)}
    assert rows["alpha note"]["usage_count"] == 1
    assert rows["beta note"]["usage_count"] == 0


def test_memory_tool_read_action_returns_json(memory_repo):
    store = MemoryStore()
    store.add("facts", "Broad pitfall", origin_session="sess-a")

    payload = json.loads(memory_tool("read", "facts", session_id="sess-b"))

    assert payload["success"] is True
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["content"] == "Broad pitfall"
    assert payload["entries"][0]["origin_session"] == "sess-a"
    assert memory_repo.active_entries_ordered("facts")[0]["usage_count"] == 1


def test_memory_tool_add_stamps_the_injected_session(memory_repo):
    memory_tool("add", "memory", "a note", session_id="sess-tool")

    assert memory_repo.active_entries_ordered("memory")[0]["origin_session"] == "sess-tool"


def test_memory_tool_prefers_an_explicit_origin_over_the_state_session(memory_repo):
    memory_tool(
        "add",
        "memory",
        "salvaged note",
        session_id="sess-derived",
        origin_session="sess-real",
    )

    assert memory_repo.active_entries_ordered("memory")[0]["origin_session"] == "sess-real"


def test_facts_eviction_is_usage_aware(memory_repo):
    store = MemoryStore(facts_char_limit=100)
    store.add("facts", "a" * 30)
    store.add("facts", "b" * 30)
    store.add("facts", "c" * 30)
    # "a" is old but frequently used; "b" is never used.
    entry_a = memory_repo.active_entries_ordered("facts")[0]
    for _ in range(3):
        memory_repo.increment_usage(entry_a["id"])

    store.add("facts", "d" * 30)

    assert store.facts_entries == ["a" * 30, "c" * 30, "d" * 30]


def test_rejected_oversized_facts_add_keeps_every_existing_entry(memory_repo):
    store = MemoryStore(facts_char_limit=100)
    store.add("facts", "a" * 40)
    store.add("facts", "b" * 40)

    rejected = store.add("facts", "x" * 120)

    assert rejected["success"] is False
    assert store.facts_entries == ["a" * 40, "b" * 40]
    assert [entry["archived"] for entry in memory_repo.list_entries("facts", limit=None)] == [0, 0]


def test_prune_unused_is_reachable_from_the_store_settings(memory_repo):
    store = MemoryStore()
    store.update_settings({"max_unused_days": 7})
    assert store.max_unused_days == 7
    assert store.effective_limits()["max_unused_days"] == 7


def test_update_settings_rejects_non_positive_values(memory_repo):
    store = MemoryStore()
    with pytest.raises(ValueError):
        store.update_settings({"memory_char_limit": 0})
