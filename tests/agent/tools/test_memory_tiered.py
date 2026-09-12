"""Unit tests for agent/tools/memory_tiered.py — the LT-1 structured facts layer."""

import asyncio
import json
from pathlib import Path

import pytest

import agent.tools.memory_tiered as memory_tiered
from config.features import TIERED_MEMORY
from agent.tools.memory import memory_store as real_memory_store
from agent.tools.memory_tiered import TieredMemoryStore


pytestmark = [pytest.mark.unit]

_CATEGORIES = TIERED_MEMORY["facts_categories"]


def _store(tmp_path: Path) -> TieredMemoryStore:
    """Build a TieredMemoryStore backed by tmp_path and a real MemoryStore."""
    return TieredMemoryStore(real_memory_store, facts_dir=tmp_path)


def _run_tool(**kwargs: object) -> str:
    """Drive the real MemoryTool coroutine and return its JSON string."""
    from agent.tools.memory import build_memory_tool

    tool = build_memory_tool()
    return asyncio.run(tool._arun(**kwargs))


# ============================================================================
# add_fact
# ============================================================================


class TestAddFact:
    """add_fact writes, deduplicates, truncates, and rejects bad input."""

    def test_add_fact_writes_correct_category(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = store.add_fact("environment", "Python 3.13 on Linux")
        assert result["success"] is True
        assert (tmp_path / "environment.md").read_text(encoding="utf-8") == "Python 3.13 on Linux"

    def test_add_fact_dedup(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("environment", "fact A")
        before = (tmp_path / "environment.md").read_text(encoding="utf-8")
        result = store.add_fact("environment", "fact A")
        assert result["success"] is True
        assert "already exists" in str(result["message"])
        assert (tmp_path / "environment.md").read_text(encoding="utf-8") == before

    def test_add_fact_capacity_truncation(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        oldest = "OLDEST " + "a" * 1493
        middle = "MIDDLE " + "b" * 1493
        newest = "NEWEST " + "c" * 1493
        store.add_fact("environment", oldest)
        store.add_fact("environment", middle)
        result = store.add_fact("environment", newest)

        content = (tmp_path / "environment.md").read_text(encoding="utf-8")
        assert len(content) <= TIERED_MEMORY["facts_char_limit"]
        assert "OLDEST" not in content
        assert "MIDDLE" in content
        assert "NEWEST" in content
        assert result["entry_count"] == 2

    def test_add_fact_unknown_category(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = store.add_fact("nonsense", "some fact")
        assert result["success"] is False
        assert "Unknown category" in str(result["error"])

    def test_add_fact_empty_content(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = store.add_fact("environment", "   ")
        assert result["success"] is False
        assert "empty" in str(result["error"]).lower()


# ============================================================================
# read_facts
# ============================================================================


class TestReadFacts:
    """read_facts resolves a single category, all categories, and empties."""

    def test_read_facts_single_category(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("project", "Use uv for dependency management")
        assert store.read_facts("project") == {"project": "Use uv for dependency management"}

    def test_read_facts_all(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("environment", "env fact")
        data = store.read_facts()
        assert set(data.keys()) == set(_CATEGORIES)
        assert data["environment"] == "env fact"

    def test_read_facts_empty_file(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.read_facts("tool_lessons") == {"tool_lessons": ""}


# ============================================================================
# search_facts
# ============================================================================


class TestSearchFacts:
    """search_facts matches substrings case-insensitively across categories."""

    def test_search_facts_case_insensitive(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("environment", "Python 3.13 on Linux")
        results = store.search_facts("PYTHON")
        assert len(results) == 1
        assert results[0]["fact"] == "Python 3.13 on Linux"

    def test_search_facts_no_match(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("environment", "Python 3.13 on Linux")
        assert store.search_facts("rust") == []

    def test_search_facts_multiple_categories(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("environment", "python runtime")
        store.add_fact("project", "Python project conventions")
        results = store.search_facts("python")
        assert {r["category"] for r in results} == {"environment", "project"}


# ============================================================================
# get_facts_listing
# ============================================================================


class TestFactsListing:
    """get_facts_listing summarises only non-empty category files."""

    def test_get_facts_listing_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_facts_listing() == ""

    def test_get_facts_listing_non_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.add_fact("environment", "first")
        store.add_fact("environment", "second")
        store.add_fact("project", "convention")
        listing = store.get_facts_listing()
        assert "environment (2 entries)" in listing
        assert "project (1 entries)" in listing
        assert "decisions" not in listing


# ============================================================================
# memory tool integration
# ============================================================================


class TestMemoryToolFactActions:
    """The real memory tool dispatches fact_* actions to the tiered store."""

    def _install_store(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        store = TieredMemoryStore(real_memory_store, facts_dir=tmp_path)
        monkeypatch.setattr(memory_tiered, "tiered_store", store)

    def test_memory_tool_fact_add(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._install_store(monkeypatch, tmp_path)
        raw = _run_tool(action="fact_add", target="environment", content="Python 3.13")
        data = json.loads(raw)
        assert data["success"] is True
        assert (tmp_path / "environment.md").read_text(encoding="utf-8") == "Python 3.13"

    def test_memory_tool_fact_read(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._install_store(monkeypatch, tmp_path)
        (tmp_path / "environment.md").write_text("Python 3.13", encoding="utf-8")
        raw = _run_tool(action="fact_read", target="environment")
        data = json.loads(raw)
        assert data["success"] is True
        assert data["facts"]["environment"] == "Python 3.13"

    def test_memory_tool_fact_search(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._install_store(monkeypatch, tmp_path)
        (tmp_path / "environment.md").write_text("Python 3.13", encoding="utf-8")
        (tmp_path / "project.md").write_text("Python conventions", encoding="utf-8")
        raw = _run_tool(action="fact_search", content="python")
        data = json.loads(raw)
        assert data["success"] is True
        assert data["count"] == 2
        assert len(data["results"]) == 2
