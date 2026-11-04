"""Tests for the ``facts`` target of ``MemoryStore``.

FACTS holds SYSTEM-LEVEL / CROSS-PROJECT pitfalls and conventions. It uses the
same machinery as ``memory`` / ``user`` (SQLite rows, per-target char cap, a
frozen prompt snapshot) with one FACTS-specific rule: an over-limit ``add``
rolls the LEAST-ACTIVE entries off instead of rejecting, because the store is
nudge-maintained and high-churn. "Least active" is
``COALESCE(last_used, created_at)``, then ``usage_count`` — never plain
oldest-first.

The store is backed by a per-test SQLite database so the real workspace memory
is never touched.
"""

import pytest

import agent.tools.memory as memory_module
from agent.tools.memory import ENTRY_DELIMITER, MemoryStore
from context_engine.store.memory_entry_repository import MemoryEntryRepository

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def memory_repo(tmp_path, monkeypatch):
    """Point the module-wide repository at an isolated temp database."""
    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "memory_entry_repo", repo)
    yield repo
    repo.close()


# ===========================================================================
# add / replace / remove
# ===========================================================================


def test_facts_add_replace_remove_roundtrip(memory_repo):
    store = MemoryStore()

    assert store.add("facts", "Broad pitfall A")["success"] is True
    assert store.add("facts", "Broad pitfall B")["success"] is True
    assert store.facts_entries == ["Broad pitfall A", "Broad pitfall B"]
    assert memory_repo.total_char_count("facts") == len("Broad pitfall A") + len("Broad pitfall B")

    replaced = store.replace("facts", "pitfall B", "Broad pitfall B updated")
    assert replaced["success"] is True
    assert store.facts_entries == ["Broad pitfall A", "Broad pitfall B updated"]

    removed = store.remove("facts", "pitfall A")
    assert removed["success"] is True
    assert store.facts_entries == ["Broad pitfall B updated"]

    # Other targets are untouched by facts writes.
    assert store.memory_entries == []
    assert store.user_entries == []
    assert memory_repo.list_entries("memory", limit=None) == []
    assert memory_repo.list_entries("user", limit=None) == []


def test_facts_add_dedup(memory_repo):
    store = MemoryStore()
    store.add("facts", "same pitfall")

    again = store.add("facts", "same pitfall")

    assert again["success"] is True
    assert again["message"] == "Entry already exists (no duplicate added)."
    assert store.facts_entries == ["same pitfall"]


# ===========================================================================
# Limit enforcement + usage-aware rollover
# ===========================================================================


def test_facts_add_rolls_least_used_entries_when_over_limit(memory_repo):
    store = MemoryStore(facts_char_limit=100)
    store.add("facts", "a" * 30)
    store.add("facts", "b" * 30)
    store.add("facts", "c" * 30)

    overflow = store.add("facts", "d" * 30)

    assert overflow["success"] is True
    assert "evicted 1 least-used entries" in overflow["message"]
    assert store.facts_entries == ["b" * 30, "c" * 30, "d" * 30]


def test_facts_eviction_prefers_an_unused_entry_over_older_used_ones(memory_repo):
    store = MemoryStore(facts_char_limit=100)
    store.add("facts", "a" * 30)
    store.add("facts", "b" * 30)
    store.add("facts", "c" * 30)

    # Mark the OLDEST entry used; the never-used middle entry is now the least
    # active one and must be evicted instead of "a".
    store.read("facts", "a" * 10)
    store.add("facts", "d" * 30)

    assert store.facts_entries == ["a" * 30, "c" * 30, "d" * 30]
    archived = memory_repo.list_entries("facts", include_archived=True, limit=None)
    assert [row["archived"] for row in archived].count(1) == 1


def test_facts_add_rejects_entry_larger_than_limit(memory_repo):
    store = MemoryStore(facts_char_limit=10)

    result = store.add("facts", "x" * 20)

    assert result["success"] is False
    assert "exceed the limit" in result["error"]
    assert memory_repo.list_entries("facts", limit=None) == []


def test_memory_target_still_rejects_on_overflow(memory_repo):
    store = MemoryStore(memory_char_limit=10)

    result = store.add("memory", "x" * 20)

    assert result["success"] is False
    assert "exceed the limit" in result["error"]


# ===========================================================================
# Load + live content + prompt snapshot
# ===========================================================================


def test_load_from_empty_store_produces_no_block(memory_repo):
    store = MemoryStore()

    store.load_from_store()

    assert store.format_for_system_prompt("facts") is None
    assert store.format_for_system_prompt("memory") is None
    assert store.format_for_system_prompt("user") is None


def test_facts_snapshot_injected_after_load(memory_repo):
    store = MemoryStore()
    store.add("facts", "Windows path separator caution")
    store.load_from_store()

    block = store.format_for_system_prompt("facts")

    assert block is not None
    assert "FACTS" in block
    assert "Windows path separator caution" in block


def test_format_live_content_reads_store(memory_repo):
    store = MemoryStore()
    assert store.format_live_content("facts") == ""

    store.add("facts", "live pitfall")
    store.facts_entries = []

    # The live view queries SQLite, not the (deliberately cleared) cache.
    assert store.format_live_content("facts") == "live pitfall"


def test_snapshot_remains_frozen_until_reload(memory_repo):
    store = MemoryStore()
    store.add("facts", "first")
    store.load_from_store()
    assert "second" not in (store.format_for_system_prompt("facts") or "")

    store.add("facts", "second")

    assert "second" not in (store.format_for_system_prompt("facts") or "")
    assert store.format_live_content("facts") == ENTRY_DELIMITER.join(["first", "second"])


# ===========================================================================
# Tool dispatch
# ===========================================================================


def test_memory_tool_accepts_facts_target(memory_repo):
    store = memory_module.memory_store
    store.load_from_store()

    result = memory_module.memory_tool("add", "facts", "via tool")

    assert '"success": true' in result
    assert store.facts_entries == ["via tool"]
    assert [entry["content"] for entry in memory_repo.list_entries("facts", limit=None)] == [
        "via tool"
    ]


def test_memory_tool_rejects_unknown_target():
    result = memory_module.memory_tool("add", "nope", "x")

    assert "Invalid target 'nope'" in result
    assert "'facts'" in result
