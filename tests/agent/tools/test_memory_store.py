"""Tests for the category-routing batch append of ``MemoryStore``.

``append_entries`` must route ``User:``-prefixed entries to the ``user`` target
and every other entry to ``memory``, each target obeying its own char limit,
dedup and oldest-first eviction. The store is backed by a per-test SQLite
database so the real workspace memory is never touched.
"""

import pytest

import agent.tools.memory as memory_module
from agent.tools.memory import MemoryStore
from context_engine.store.memory_entry_repository import MemoryEntryRepository

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def memory_repo(tmp_path, monkeypatch):
    """Point the module-wide repository at an isolated temp database."""
    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "memory_entry_repo", repo)
    yield repo
    repo.close()


class TestEntryCountCap:
    """A count cap complements the char budgets (tiny entries are cheap)."""

    def test_add_refuses_at_the_cap(self, memory_repo, monkeypatch):
        monkeypatch.setitem(memory_module.MEMORY_TOOL, "max_entries_per_target", 2)
        store = MemoryStore()
        assert store.add("memory", "alpha")["success"] is True
        assert store.add("memory", "beta")["success"] is True

        refused = store.add("memory", "gamma")

        assert refused["success"] is False
        assert "entries" in refused["error"]
        assert store.memory_entries == ["alpha", "beta"]

    def test_append_evicts_oldest_at_the_cap(self, memory_repo, monkeypatch):
        monkeypatch.setitem(memory_module.MEMORY_TOOL, "max_entries_per_target", 2)
        store = MemoryStore()
        store.append_entries("§ alpha\n§ beta")

        store.append_entries("§ gamma")

        assert store.memory_entries == ["beta", "gamma"]

    def test_facts_evicts_least_active_at_the_cap(self, memory_repo, monkeypatch):
        monkeypatch.setitem(memory_module.MEMORY_TOOL, "max_entries_per_target", 2)
        store = MemoryStore()
        store.add("facts", "old fact")
        store.add("facts", "recent fact")

        result = store.add("facts", "new fact")

        assert result["success"] is True
        assert len(store.facts_entries) == 2
        # FACTS rolls the least-active entry off, never refusing the write.
        assert "old fact" not in store.facts_entries
        assert "new fact" in store.facts_entries


# ===========================================================================
# Routing
# ===========================================================================


def test_append_entries_memory_only(memory_repo):
    store = MemoryStore()
    result = store.append_entries("§ alpha\n§ Environment: Python 3.13")

    assert result["success"] is True
    assert store.memory_entries == ["alpha", "Environment: Python 3.13"]
    assert store.user_entries == []
    assert "to memory" in result["message"]
    assert "to user" not in result["message"]
    assert [entry["target"] for entry in memory_repo.list_entries(limit=None)] == [
        "memory",
        "memory",
    ]


def test_append_entries_user_only(memory_repo):
    store = MemoryStore()
    result = store.append_entries("§ User: prefers concise answers")

    assert result["success"] is True
    assert store.user_entries == ["User: prefers concise answers"]
    assert store.memory_entries == []
    assert "to user" in result["message"]
    assert len(memory_repo.list_entries("user", limit=None)) == 1
    assert memory_repo.list_entries("memory", limit=None) == []


def test_append_entries_user_prefix_case_insensitive_with_spaces(memory_repo):
    store = MemoryStore()
    store.append_entries("§ USER: a\n§ user : b\n§ User: c")

    assert store.user_entries == ["USER: a", "user : b", "User: c"]
    assert store.memory_entries == []


def test_append_entries_mixed(memory_repo):
    store = MemoryStore()
    result = store.append_entries(
        "§ Environment: Python 3.13\n§ User: likes tea\n§ Project: uses uv"
    )

    assert result["success"] is True
    assert store.memory_entries == ["Environment: Python 3.13", "Project: uses uv"]
    assert store.user_entries == ["User: likes tea"]
    assert result["entry_count"] == 3
    assert "to memory, user" in result["message"]


# ===========================================================================
# Dedup / eviction / empty
# ===========================================================================


def test_append_entries_dedup_per_target(memory_repo):
    store = MemoryStore()
    store.append_entries("§ User: likes tea\n§ Environment: Python 3.13")

    second = store.append_entries("§ User: likes tea\n§ Project: uses uv")
    assert store.user_entries == ["User: likes tea"]
    assert store.memory_entries == ["Environment: Python 3.13", "Project: uses uv"]
    assert "deduplicated 1" in second["message"]
    assert second["entry_count"] == 3


def test_append_entries_user_limit_evicts_oldest(memory_repo):
    store = MemoryStore(user_char_limit=100)
    store.append_entries("§ User: " + "a" * 30 + "\n§ User: " + "b" * 30)
    store.append_entries("§ User: " + "c" * 30)
    store.append_entries("§ User: " + "d" * 30)

    assert store.user_entries == [
        "User: " + "c" * 30,
        "User: " + "d" * 30,
    ]
    # The memory target is untouched: the user limit never bleeds into it.
    assert store.memory_entries == []
    assert memory_repo.list_entries("memory", limit=None) == []


def test_append_entries_empty(memory_repo):
    store = MemoryStore()
    result = store.append_entries("   ")

    assert result == {"success": True, "message": "No entries to add."}
    assert store.memory_entries == []
    assert store.user_entries == []


def test_append_entries_all_existing(memory_repo):
    store = MemoryStore()
    store.append_entries("§ User: likes tea\n§ Environment: Python 3.13")

    again = store.append_entries("§ User: likes tea\n§ Environment: Python 3.13")
    assert again["success"] is True
    assert again["message"] == "All entries already exist (no duplicates added)."
    assert store.user_entries == ["User: likes tea"]
    assert store.memory_entries == ["Environment: Python 3.13"]
