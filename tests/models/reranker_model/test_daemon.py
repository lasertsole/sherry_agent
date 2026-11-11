"""The reranker daemon's HTTP contract and its client-side strategy.

A fake ``CrossEncoderGGUF`` backs a real loopback server (no GGUF download); the
client tests fake the shared lifecycle and ``request_json`` to pin the payloads,
the one-retry respawn and the in-process fallback.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
import pytest
import requests

import models.reranker_model as reranker
import models.reranker_model.daemon as daemon
from models.daemon.base import DaemonError, ReadyState

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeModel:
    def __init__(self) -> None:
        self.rank_calls: list[tuple] = []
        self.filter_calls: list[tuple] = []
        self.score_calls: list[list[tuple[str, str]]] = []

    def rank(self, query, documents, top_k=None, gap_score=None):
        self.rank_calls.append((query, documents, top_k, gap_score))
        rows = [
            {"rank": index + 1, "corpus_id": index, "document": text, "score": 0.9}
            for index, text in enumerate(documents)
        ]
        return rows[:top_k] if top_k else rows

    def filter(self, query, documents, gap_score=0.5):
        self.filter_calls.append((query, documents, gap_score))
        return list(documents)

    def predict_scores(self, pairs):
        self.score_calls.append(list(pairs))
        return [0.25 for _ in pairs]


class _NoopWatchdog:
    def touch(self) -> None:
        pass


@pytest.fixture()
def live_daemon(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(daemon, "_model", fake)
    monkeypatch.setattr(daemon._Handler, "watchdog", _NoopWatchdog(), raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), daemon._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_health_reports_ready(live_daemon):
    base, _fake = live_daemon
    assert requests.get(f"{base}/healthy", timeout=5).json() == {"status": "ok", "ready": True}


def test_rerank_returns_the_model_rows(live_daemon):
    base, fake = live_daemon

    response = requests.post(
        f"{base}/rerank",
        json={"query": "q", "documents": ["a", "b"], "top_k": 1},
        timeout=5,
    )

    assert response.status_code == 200
    assert response.json()["result"] == [{"rank": 1, "corpus_id": 0, "document": "a", "score": 0.9}]
    assert fake.rank_calls == [("q", ["a", "b"], 1, None)]


def test_filter_and_predict(live_daemon):
    base, fake = live_daemon

    assert requests.post(
        f"{base}/filter",
        json={"query": "q", "documents": ["a"], "gap_score": 0.7},
        timeout=5,
    ).json()["result"] == ["a"]
    assert requests.post(
        f"{base}/predict",
        json={"pairs": [["q", "a"], ["q", "b"]]},
        timeout=5,
    ).json()["scores"] == [0.25, 0.25]

    assert fake.filter_calls == [("q", ["a"], 0.7)]
    assert fake.score_calls == [[("q", "a"), ("q", "b")]]


def test_invalid_requests_are_refused(live_daemon):
    base, fake = live_daemon

    for path, payload in (
        ("/rerank", {"query": "", "documents": ["a"]}),
        ("/rerank", {"query": "q", "documents": ["a"], "top_k": True}),
        ("/rerank", {"query": "q", "documents": "a"}),
        ("/filter", {"query": "q", "documents": ["a"], "gap_score": "high"}),
        ("/predict", {"pairs": [["only-one"]]}),
    ):
        assert requests.post(f"{base}{path}", json=payload, timeout=5).status_code == 500

    assert fake.rank_calls == [] and fake.filter_calls == [] and fake.score_calls == []


def test_unknown_path_does_not_load_the_model(live_daemon, monkeypatch):
    base, _fake = live_daemon
    monkeypatch.setattr(daemon, "_model", None)
    monkeypatch.setattr(daemon, "_get_model", lambda: pytest.fail("must not load"))

    assert requests.post(f"{base}/nope", json={}, timeout=5).status_code == 404


def _wire(monkeypatch, responses):
    state = {"ready": ReadyState.READY}
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(reranker, "ensure_ready", lambda *a, **k: state["ready"])

    def _request(_spec, path, payload, *, timeout_s):
        calls.append((path, payload))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(reranker, "request_json", _request)
    return state, calls


def test_client_rank_posts_top_k_and_gap(monkeypatch):
    backend = reranker._LocalGgufBackend()
    _state, calls = _wire(monkeypatch, [{"result": [{"rank": 1, "corpus_id": 0}]}])

    result = backend.rank("q", ["a", "b"], top_k=2, gap_score=0.4)

    assert result == [{"rank": 1, "corpus_id": 0}]
    assert calls == [
        ("/rerank", {"query": "q", "documents": ["a", "b"], "top_k": 2, "gap_score": 0.4})
    ]


def test_client_filter_defaults_gap_to_half(monkeypatch):
    backend = reranker._LocalGgufBackend()
    _state, calls = _wire(monkeypatch, [{"result": ["a"]}])

    assert backend.filter("q", ["a"]) == ["a"]
    assert calls[0][1]["gap_score"] == 0.5


def test_client_predict_uses_one_pair_per_call(monkeypatch):
    backend = reranker._LocalGgufBackend()
    _state, calls = _wire(monkeypatch, [{"scores": [0.75]}])

    assert backend.predict("q", "a") == 0.75
    assert calls == [("/predict", {"pairs": [["q", "a"]]})]


def test_client_predict_scores_short_circuits_empty(monkeypatch):
    backend = reranker._LocalGgufBackend()
    _wire(monkeypatch, [])
    assert backend.predict_scores([]) == []


def test_client_rejects_a_body_without_the_expected_key(monkeypatch):
    backend = reranker._LocalGgufBackend()
    _wire(monkeypatch, [{"nope": []}])

    with pytest.raises(DaemonError, match="without 'result'"):
        backend.rank("q", ["a"])


def test_client_retries_once_after_a_daemon_error(monkeypatch):
    backend = reranker._LocalGgufBackend()
    _state, calls = _wire(
        monkeypatch,
        [DaemonError("gone"), {"result": [{"rank": 1, "corpus_id": 0}]}],
    )

    assert backend.rank("q", ["a"]) == [{"rank": 1, "corpus_id": 0}]
    assert len(calls) == 2


def test_client_falls_back_in_process_when_no_daemon_can_start(monkeypatch):
    backend = reranker._LocalGgufBackend()
    state, calls = _wire(monkeypatch, [])
    state["ready"] = ReadyState.UNAVAILABLE
    monkeypatch.setattr(
        backend, "_in_process", lambda method, *args, **kwargs: f"fallback:{method}"
    )

    assert backend.rank("q", ["a"]) == "fallback:rank"
    assert calls == []
    assert backend._daemon_usable is False


def test_client_does_not_fall_back_while_the_daemon_is_warming(monkeypatch):
    backend = reranker._LocalGgufBackend()
    state, _calls = _wire(monkeypatch, [])
    state["ready"] = ReadyState.WARMING

    with pytest.raises(DaemonError, match="warming"):
        backend.rank("q", ["a"])
