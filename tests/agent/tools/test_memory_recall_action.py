"""The memory tool's semantic ``recall`` action."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools import memory as memory_module
from agent.tools.memory import MemoryStore, MemoryActionSchema, memory_tool
from context_engine.store.memory_entry_repository import MemoryEntryRepository


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    repo = MemoryEntryRepository(tmp_path / "memory.db")
    entry = repo.add_entry("memory", "alpha deployment note")
    repo.set_embedding(int(entry["id"]), [1.0, 0.0], model="bge-m3")
    user = repo.add_entry("user", "prefers terse replies")
    repo.set_embedding(int(user["id"]), [0.9, 0.1], model="bge-m3")
    store = MemoryStore(repo=repo, semantic_recall_enabled=True)
    monkeypatch.setattr(memory_module, "memory_store", store)
    monkeypatch.setattr(
        "context_engine.embeddings.indexer._embed_fn",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    import importlib

    reranker_module = importlib.import_module("models.reranker_model")

    monkeypatch.setattr(
        reranker_module,
        "build_reranker_model",
        lambda: type("R", (), {"rank": lambda self, *a, **k: []})(),
    )
    yield store
    repo.close()


def test_recall_returns_relevant_entries(store: MemoryStore) -> None:
    payload = json.loads(memory_tool("recall", target="all", query="deployment"))
    assert payload["success"] is True
    assert payload["entry_count"] == 2
    assert {entry["content"] for entry in payload["entries"]} == {
        "alpha deployment note",
        "prefers terse replies",
    }
    assert all("score" in entry for entry in payload["entries"])


def test_recall_requires_a_query(store: MemoryStore) -> None:
    payload = json.loads(memory_tool("recall", target="all"))
    assert payload["success"] is False
    assert "query" in payload["error"]


def test_recall_allows_all_targets_but_rejects_unknown(store: MemoryStore) -> None:
    payload = json.loads(memory_tool("recall", target="bogus", query="x"))
    assert payload["success"] is False


def test_schema_accepts_recall() -> None:
    schema = MemoryActionSchema(action="recall", target="all", query="x")
    assert schema.action == "recall"
