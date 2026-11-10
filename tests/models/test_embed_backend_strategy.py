"""Embedding strategy dispatch (2026-11-09 audit M2).

``CustomEmbedding`` used to re-run ``if _backend == "local"`` inside every
public method. It now receives one backend strategy and only delegates; the
factory injects it, a direct constructor resolves it lazily.
"""

from __future__ import annotations

import pytest

from models.embed_model.text import core

pytestmark = [pytest.mark.unit]


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text))] for text in texts]


def test_injected_backend_receives_every_call():
    backend = _RecordingBackend()
    model = core.CustomEmbedding(backend=backend)

    assert model.embed_documents(["a", "bb"]) == [[1.0], [2.0]]
    assert model.embed_query("ccc") == [3.0]
    assert backend.calls == [["a", "bb"], ["ccc"]]


def test_empty_documents_short_circuit_without_the_backend():
    backend = _RecordingBackend()
    model = core.CustomEmbedding(backend=backend)

    assert model.embed_documents([]) == []
    assert backend.calls == []


def test_direct_construction_resolves_the_strategy_once(monkeypatch):
    backend = _RecordingBackend()
    calls: list[int] = []
    monkeypatch.setattr(core, "_build_embedding_backend", lambda: calls.append(1) or backend)

    model = core.CustomEmbedding()

    assert model.embed_query("a") == [1.0]
    assert model.embed_query("bb") == [2.0]
    assert calls == [1], "the strategy must be resolved once, not per call"


def test_factory_injects_the_selected_strategy(monkeypatch):
    backend = _RecordingBackend()
    monkeypatch.setattr(core, "setup_embed_model", lambda: None)
    monkeypatch.setattr(core, "_build_embedding_backend", lambda: backend)

    model = core.build_embed_model()

    assert model._backend is backend
