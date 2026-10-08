"""Tests for the category-routing batch append of ``MemoryStore`` (Memory Flush).

``append_entries`` must route ``User:``-prefixed entries to USER.md and every
other entry to MEMORY.md, each file obeying its own char limit, dedup and
oldest-first eviction. The store is pointed at a per-test tmp directory so the
real ``workspace/MEMORY.md`` / ``workspace/USER.md`` are never touched.
"""

import pytest

import agent.tools.memory as memory_module
from agent.tools.memory import MemoryStore

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def tmp_memory_dir(tmp_path, monkeypatch):
    """Point the real MemoryStore at an isolated tmp memory directory."""
    mem_dir = tmp_path / "memory"
    monkeypatch.setattr(memory_module, "MEMORY_DIR", mem_dir)
    return mem_dir


def _read(mem_dir, name: str) -> str:
    path = mem_dir / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ===========================================================================
# Routing
# ===========================================================================


def test_append_entries_memory_only(tmp_memory_dir):
    store = MemoryStore()
    result = store.append_entries("§ alpha\n§ Environment: Python 3.13")

    assert result["success"] is True
    assert store.memory_entries == ["alpha", "Environment: Python 3.13"]
    assert store.user_entries == []
    assert "MEMORY.md" in result["message"]
    assert "USER.md" not in result["message"]
    assert _read(tmp_memory_dir, "MEMORY.md") == "alpha\n§\nEnvironment: Python 3.13"
    assert not (tmp_memory_dir / "USER.md").exists()


def test_append_entries_user_only(tmp_memory_dir):
    store = MemoryStore()
    result = store.append_entries("§ User: prefers concise answers")

    assert result["success"] is True
    assert store.user_entries == ["User: prefers concise answers"]
    assert store.memory_entries == []
    assert "USER.md" in result["message"]
    assert _read(tmp_memory_dir, "USER.md") == "User: prefers concise answers"
    assert not (tmp_memory_dir / "MEMORY.md").exists()


def test_append_entries_user_prefix_case_insensitive_with_spaces(tmp_memory_dir):
    store = MemoryStore()
    store.append_entries("§ USER: a\n§ user : b\n§ User: c")

    assert store.user_entries == ["USER: a", "user : b", "User: c"]
    assert store.memory_entries == []


def test_append_entries_mixed(tmp_memory_dir):
    store = MemoryStore()
    result = store.append_entries(
        "§ Environment: Python 3.13\n§ User: likes tea\n§ Project: uses uv"
    )

    assert result["success"] is True
    assert store.memory_entries == ["Environment: Python 3.13", "Project: uses uv"]
    assert store.user_entries == ["User: likes tea"]
    assert result["entry_count"] == 3
    assert "MEMORY.md" in result["message"]
    assert "USER.md" in result["message"]


# ===========================================================================
# Dedup / eviction / empty
# ===========================================================================


def test_append_entries_dedup_per_target(tmp_memory_dir):
    store = MemoryStore()
    store.append_entries("§ User: likes tea\n§ Environment: Python 3.13")

    second = store.append_entries("§ User: likes tea\n§ Project: uses uv")
    assert store.user_entries == ["User: likes tea"]
    assert store.memory_entries == ["Environment: Python 3.13", "Project: uses uv"]
    assert "deduplicated 1" in second["message"]
    assert second["entry_count"] == 3


def test_append_entries_user_limit_evicts_oldest(tmp_memory_dir):
    store = MemoryStore(user_char_limit=100)
    store.append_entries("§ User: " + "a" * 30 + "\n§ User: " + "b" * 30)
    store.append_entries("§ User: " + "c" * 30)
    store.append_entries("§ User: " + "d" * 30)

    assert store.user_entries == [
        "User: " + "c" * 30,
        "User: " + "d" * 30,
    ]
    # MEMORY.md untouched: the user limit never bleeds into memory entries.
    assert store.memory_entries == []
    assert not (tmp_memory_dir / "MEMORY.md").exists()


def test_append_entries_empty(tmp_memory_dir):
    store = MemoryStore()
    result = store.append_entries("   ")

    assert result == {"success": True, "message": "No entries to add."}
    assert store.memory_entries == []
    assert store.user_entries == []


def test_append_entries_all_existing(tmp_memory_dir):
    store = MemoryStore()
    store.append_entries("§ User: likes tea\n§ Environment: Python 3.13")

    again = store.append_entries("§ User: likes tea\n§ Environment: Python 3.13")
    assert again["success"] is True
    assert again["message"] == "All entries already exist (no duplicates added)."
    assert store.user_entries == ["User: likes tea"]
    assert store.memory_entries == ["Environment: Python 3.13"]
