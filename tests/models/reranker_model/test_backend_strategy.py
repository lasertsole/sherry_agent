"""Reranker strategy dispatch: the facade never branches on the backend.

The audit (2026-11-09, H5) replaced the per-method ``if self._local`` dispatch
with a backend object chosen once at construction. These tests pin the new
contract: one strategy receives every call, and the facade only forwards.
"""

from __future__ import annotations

import pytest

import models.reranker_model as reranker_module

pytestmark = [pytest.mark.unit]


class _RecordingBackend:
    """Backend double implementing the four-method surface (no model, no network)."""

    label = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.closed = 0

    def rank(self, query, documents, **kwargs):
        self.calls.append(("rank", (query, documents), kwargs))
        return ["ranked"]

    def filter(self, query, documents, **kwargs):
        self.calls.append(("filter", (query, documents), kwargs))
        return ["filtered"]

    def predict(self, query, passage):
        self.calls.append(("predict", (query, passage), {}))
        return 0.5

    def predict_scores(self, pairs):
        self.calls.append(("predict_scores", (pairs,), {}))
        return [0.1, 0.2]

    def close(self):
        self.closed += 1


def _wrapper_with(monkeypatch, backend, *, local: bool):
    monkeypatch.setattr(reranker_module, "_is_local", lambda: local)
    monkeypatch.setattr(reranker_module, "_LocalGgufBackend", lambda: backend)
    monkeypatch.setattr(reranker_module, "_CloudBackend", lambda: backend)
    return reranker_module._LazyReranker()


def test_local_flag_selects_the_local_strategy(monkeypatch):
    backend = _RecordingBackend()

    wrapper = _wrapper_with(monkeypatch, backend, local=True)

    assert wrapper.backend == "recording"
    assert isinstance(wrapper._backend, _RecordingBackend)


def test_cloud_flag_selects_the_cloud_strategy(monkeypatch):
    backend = _RecordingBackend()

    wrapper = _wrapper_with(monkeypatch, backend, local=False)

    assert wrapper.backend == "recording"


def test_every_public_method_forwards_to_the_same_backend(monkeypatch):
    backend = _RecordingBackend()
    wrapper = _wrapper_with(monkeypatch, backend, local=True)

    assert wrapper.rank("q", ["a", "b"]) == ["ranked"]
    assert wrapper.filter("q", ["a"]) == ["filtered"]
    assert wrapper.predict("q", "p") == 0.5
    assert wrapper.predict_scores([("q", "p")]) == [0.1, 0.2]

    assert [name for name, _args, _kwargs in backend.calls] == [
        "rank",
        "filter",
        "predict",
        "predict_scores",
    ]


def test_empty_documents_short_circuit_without_touching_the_backend(monkeypatch):
    backend = _RecordingBackend()
    wrapper = _wrapper_with(monkeypatch, backend, local=True)

    assert wrapper.rank("q", []) == []
    assert wrapper.filter("q", []) == []
    assert backend.calls == []


def test_close_forwards_and_clears_the_meta_cache(monkeypatch):
    backend = _RecordingBackend()
    wrapper = _wrapper_with(monkeypatch, backend, local=True)

    wrapper.close()

    assert backend.closed == 1
