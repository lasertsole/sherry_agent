"""Lazy embedding index over long-term memory entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_engine.store.memory_embeddings import (
    MEMORY_EMBED_MODEL,
    index_pending_memory_entries,
    load_memory_embeddings,
)
from context_engine.store.memory_entry_repository import MemoryEntryRepository


@pytest.fixture()
def repo(tmp_path: Path) -> MemoryEntryRepository:
    repository = MemoryEntryRepository(tmp_path / "memory.db")
    yield repository
    repository.close()


def _fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    def _embed(texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0, 0.5] for text in texts]

    monkeypatch.setattr("context_engine.store.memory_embeddings._get_embed_fn", lambda: _embed)


def test_index_pending_embeds_active_entries_once(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_embedder(monkeypatch)
    first = repo.add_entry("memory", "alpha")
    second = repo.add_entry("user", "beta")
    archived = repo.add_entry("facts", "gamma")
    repo.archive_entry(int(archived["id"]), "manual")

    assert index_pending_memory_entries(repo) == 2
    assert repo.count_embeddings() == 2
    rows = {int(entry["id"]): vector for entry, vector in repo.list_with_embeddings()}
    assert set(rows) == {int(first["id"]), int(second["id"])}
    assert all(vector for vector in rows.values())
    assert index_pending_memory_entries(repo) == 0


def test_editing_an_entry_invalidates_its_vector(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_embedder(monkeypatch)
    entry = repo.add_entry("memory", "before")
    assert index_pending_memory_entries(repo) == 1
    assert repo.count_embeddings() == 1

    repo.update_entry(int(entry["id"]), content="after")
    assert repo.count_embeddings() == 0
    assert index_pending_memory_entries(repo) == 1
    loaded = load_memory_embeddings(repo=repo)
    assert loaded[0]["content"] == "after"
    assert loaded[0]["embedding"] == [5.0, 1.0, 0.5]


def test_load_filters_by_target(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_embedder(monkeypatch)
    repo.add_entry("memory", "a")
    repo.add_entry("user", "bb")
    index_pending_memory_entries(repo)

    assert [row["target"] for row in load_memory_embeddings("user", repo)] == ["user"]
    assert len(load_memory_embeddings(None, repo)) == 2
    assert MEMORY_EMBED_MODEL == "bge-m3"
