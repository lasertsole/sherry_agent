"""Tests for the ``facts`` target of ``MemoryStore`` (the third memory file).

FACTS.md holds broad, module-independent pitfalls and conventions. It uses the
same machinery as MEMORY.md / USER.md (single file, ``§``-delimited entries,
per-file char cap, atomic rename writes, frozen prompt snapshot) with one
FACTS-specific rule: an over-limit ``add`` rolls the oldest entries off instead
of rejecting, because the file is nudge-maintained and high-churn.

The store is pointed at a per-test tmp directory so the real
``workspace/memory/FACTS.md`` is never touched.
"""

import pytest

import agent.tools.memory as memory_module
from agent.tools.memory import ENTRY_DELIMITER, MemoryStore

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
# Path + add / replace / remove
# ===========================================================================


def test_path_for_facts(tmp_memory_dir):
    assert MemoryStore._path_for("facts") == tmp_memory_dir / "FACTS.md"


def test_facts_add_replace_remove_roundtrip(tmp_memory_dir):
    store = MemoryStore()

    assert store.add("facts", "Broad pitfall A")["success"] is True
    assert store.add("facts", "Broad pitfall B")["success"] is True
    assert store.facts_entries == ["Broad pitfall A", "Broad pitfall B"]
    assert _read(tmp_memory_dir, "FACTS.md") == "Broad pitfall A\n§\nBroad pitfall B"

    replaced = store.replace("facts", "pitfall B", "Broad pitfall B updated")
    assert replaced["success"] is True
    assert store.facts_entries == ["Broad pitfall A", "Broad pitfall B updated"]

    removed = store.remove("facts", "pitfall A")
    assert removed["success"] is True
    assert store.facts_entries == ["Broad pitfall B updated"]
    assert _read(tmp_memory_dir, "FACTS.md") == "Broad pitfall B updated"

    # Other stores are untouched by facts writes.
    assert not (tmp_memory_dir / "MEMORY.md").exists()
    assert not (tmp_memory_dir / "USER.md").exists()


def test_facts_add_dedup(tmp_memory_dir):
    store = MemoryStore()
    store.add("facts", "same pitfall")

    again = store.add("facts", "same pitfall")

    assert again["success"] is True
    assert again["message"] == "Entry already exists (no duplicate added)."
    assert store.facts_entries == ["same pitfall"]


# ===========================================================================
# Limit enforcement + oldest-first rollover
# ===========================================================================


def test_facts_add_rolls_oldest_entries_when_over_limit(tmp_memory_dir):
    store = MemoryStore(facts_char_limit=100)
    store.add("facts", "a" * 30)
    store.add("facts", "b" * 30)
    store.add("facts", "c" * 30)

    overflow = store.add("facts", "d" * 30)

    assert overflow["success"] is True
    assert "evicted 1 oldest entries" in overflow["message"]
    assert store.facts_entries == ["b" * 30, "c" * 30, "d" * 30]
    assert _read(tmp_memory_dir, "FACTS.md") == ENTRY_DELIMITER.join(store.facts_entries)


def test_facts_add_rejects_entry_larger_than_limit(tmp_memory_dir):
    store = MemoryStore(facts_char_limit=10)

    result = store.add("facts", "x" * 20)

    assert result["success"] is False
    assert "exceed the limit" in result["error"]
    assert not (tmp_memory_dir / "FACTS.md").exists()


def test_memory_target_still_rejects_on_overflow(tmp_memory_dir):
    store = MemoryStore(memory_char_limit=10)

    result = store.add("memory", "x" * 20)

    assert result["success"] is False
    assert "exceed the limit" in result["error"]


# ===========================================================================
# Atomic write
# ===========================================================================


def test_atomic_write_leaves_no_temp_files(tmp_memory_dir):
    store = MemoryStore()
    store.add("facts", "pitfall")

    leftovers = [p.name for p in tmp_memory_dir.iterdir() if p.name.startswith(".mem_")]
    assert leftovers == []
    assert _read(tmp_memory_dir, "FACTS.md") == "pitfall"


def test_atomic_write_failure_keeps_original_intact(tmp_memory_dir, monkeypatch):
    store = MemoryStore()
    store.add("facts", "original")

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(memory_module, "atomic_replace", _boom)

    with pytest.raises(RuntimeError):
        store.add("facts", "second")

    assert _read(tmp_memory_dir, "FACTS.md") == "original"
    assert [p.name for p in tmp_memory_dir.iterdir() if p.name.startswith(".mem_")] == []


# ===========================================================================
# Load + live content + prompt snapshot
# ===========================================================================


def test_load_from_disk_creates_missing_facts_file(tmp_memory_dir):
    store = MemoryStore()

    store.load_from_disk()

    assert (tmp_memory_dir / "FACTS.md").exists()
    assert (tmp_memory_dir / "FACTS.md").read_text(encoding="utf-8") == ""


def test_facts_snapshot_injected_after_load(tmp_memory_dir):
    store = MemoryStore()
    store.add("facts", "Windows path separator caution")
    store.load_from_disk()

    block = store.format_for_system_prompt("facts")

    assert block is not None
    assert "FACTS" in block
    assert "Windows path separator caution" in block


def test_empty_facts_produces_no_block(tmp_memory_dir):
    store = MemoryStore()
    store.load_from_disk()

    assert store.format_for_system_prompt("facts") is None
    assert store.format_for_system_prompt("memory") is None
    assert store.format_for_system_prompt("user") is None


def test_format_live_content_reads_disk(tmp_memory_dir):
    store = MemoryStore()
    assert store.format_live_content("facts") == ""

    store.add("facts", "live pitfall")
    store.facts_entries = []

    assert store.format_live_content("facts") == "live pitfall"


# ===========================================================================
# Tool dispatch
# ===========================================================================


def test_memory_tool_accepts_facts_target(tmp_memory_dir):
    store = memory_module.memory_store
    store.memory_entries = []
    store.user_entries = []
    store.facts_entries = []

    result = memory_module.memory_tool("add", "facts", "via tool")

    assert '"success": true' in result
    assert store.facts_entries == ["via tool"]


def test_memory_tool_rejects_unknown_target():
    result = memory_module.memory_tool("add", "nope", "x")

    assert "Invalid target 'nope'" in result
    assert "'facts'" in result
