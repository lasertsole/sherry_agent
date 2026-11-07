"""Semantic recall: cosine, reranker fallback, budget, usage and staleness."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_engine.store.memory_entry_repository import MemoryEntryRepository
from context_engine.store.memory_recall import recall_memories


@pytest.fixture()
def repo(tmp_path: Path) -> MemoryEntryRepository:
    repository = MemoryEntryRepository(tmp_path / "memory.db")
    yield repository
    repository.close()


def _seed(repo: MemoryEntryRepository) -> dict[str, int]:
    entries = {
        "alpha": repo.add_entry("memory", "alpha", compaction_epoch=1),
        "beta": repo.add_entry("user", "beta", compaction_epoch=2),
        "gamma": repo.add_entry("facts", "gamma", compaction_epoch=2),
    }
    vectors = {"alpha": [1.0, 0.0], "beta": [0.0, 1.0], "gamma": [0.7, 0.7]}
    for key, entry in entries.items():
        repo.set_embedding(int(entry["id"]), vectors[key], model="bge-m3")
    return {key: int(entry["id"]) for key, entry in entries.items()}


def _patch_query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "context_engine.embeddings.indexer._embed_fn",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )


def test_cosine_order_without_reranker(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(repo)
    _patch_query_embedding(monkeypatch)

    class _BrokenReranker:
        def rank(self, *_args, **_kwargs):
            raise RuntimeError("no local model")

    import importlib

    reranker_module = importlib.import_module("models.reranker_model")
    monkeypatch.setattr(reranker_module, "build_reranker_model", lambda: _BrokenReranker())
    results = recall_memories("alpha", repo=repo, limit=3)
    assert [row["content"] for row in results] == ["alpha", "gamma", "beta"]
    assert results[0]["score"] > results[1]["score"]


def test_reranker_reorders_and_usage_is_counted(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed(repo)
    _patch_query_embedding(monkeypatch)

    class _Reranker:
        def rank(self, query, documents, top_k=None):
            del query, documents
            return [
                {"corpus_id": 2, "score": 0.9},
                {"corpus_id": 0, "score": 0.4},
            ][:top_k]

    import importlib

    reranker_module = importlib.import_module("models.reranker_model")

    monkeypatch.setattr(reranker_module, "build_reranker_model", lambda: _Reranker())
    results = recall_memories("alpha", repo=repo, limit=2)
    assert [row["content"] for row in results] == ["beta", "alpha"]
    assert repo.get_entry(ids["beta"])["usage_count"] == 1
    assert repo.get_entry(ids["alpha"])["usage_count"] == 1
    assert repo.get_entry(ids["gamma"])["usage_count"] == 0


def test_character_budget_truncates_and_stops(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(repo)
    _patch_query_embedding(monkeypatch)
    import importlib

    reranker_module = importlib.import_module("models.reranker_model")

    monkeypatch.setattr(
        reranker_module,
        "build_reranker_model",
        lambda: type("R", (), {"rank": lambda self, *a, **k: []})(),
    )
    results = recall_memories("alpha", repo=repo, limit=3, char_budget=6)
    assert len(results) == 1
    assert results[0]["content"] == "alpha"
    assert results[0]["target"] == "memory"


def test_stale_entries_are_skipped_unless_requested(
    repo: MemoryEntryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(repo)
    _patch_query_embedding(monkeypatch)
    import importlib

    reranker_module = importlib.import_module("models.reranker_model")

    monkeypatch.setattr(
        reranker_module,
        "build_reranker_model",
        lambda: type("R", (), {"rank": lambda self, *a, **k: []})(),
    )

    class _Provider:
        def get_context_epoch(self, _session_id: str) -> tuple[int, int]:
            return 2, 1_000

    monkeypatch.setattr("runtime.data_provider.get_prompt_data_provider", lambda: _Provider())
    results = recall_memories("alpha", repo=repo, limit=3, session_id="s1")
    assert [row["content"] for row in results] == ["gamma", "beta"]
    assert all(not row["stale"] for row in results)

    with_stale = recall_memories("alpha", repo=repo, limit=3, session_id="s1", include_stale=True)
    assert [row["content"] for row in with_stale] == ["alpha", "gamma", "beta"]
    assert with_stale[0]["stale"] is True


def test_blank_query_and_empty_store(repo: MemoryEntryRepository) -> None:
    assert recall_memories("   ", repo=repo) == []
    assert recall_memories("anything", repo=repo) == []
