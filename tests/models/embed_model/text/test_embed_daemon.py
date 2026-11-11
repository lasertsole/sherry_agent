"""The embedding daemon's HTTP contract and its client-side strategy.

The daemon is exercised as a real loopback server with a fake model (no GGUF),
so the wire shape, ordering and validation are pinned; the client tests fake
the shared lifecycle and ``request_json`` to pin the request payload, the
one-retry respawn, L2 normalisation and the in-process fallback.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from typing import Any

import pytest
import requests

import models.embed_model.text.core as core
import models.embed_model.text.daemon as daemon
from models.daemon.base import DaemonError, ReadyState

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.closed = False

    def create_embedding(self, *, input: list[str]) -> dict[str, Any]:  # noqa: A002
        self.calls.append(list(input))
        # Reverse order on purpose: the handler must sort by index.
        return {
            "data": [
                {"index": index, "embedding": [float(len(text)), 1.0]}
                for index, text in reversed(list(enumerate(input)))
            ]
        }

    def close(self) -> None:
        self.closed = True


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


def test_health_reports_ready_only_when_the_model_is_loaded(live_daemon, monkeypatch):
    base, _fake = live_daemon

    assert requests.get(f"{base}/healthy", timeout=5).json() == {
        "status": "ok",
        "ready": True,
    }

    monkeypatch.setattr(daemon, "_model", None)
    assert requests.get(f"{base}/healthy", timeout=5).json() == {
        "status": "warming",
        "ready": False,
    }


def test_embeddings_batch_returns_vectors_in_input_order(live_daemon):
    base, fake = live_daemon

    response = requests.post(f"{base}/embeddings", json={"input": ["a", "bb", "ccc"]}, timeout=5)

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["index"] for item in data] == [0, 1, 2]
    assert [item["embedding"] for item in data] == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]
    assert fake.calls == [["a", "bb", "ccc"]]


def test_a_single_string_input_is_accepted(live_daemon):
    base, fake = live_daemon

    response = requests.post(f"{base}/embeddings", json={"input": "hello"}, timeout=5)

    assert response.status_code == 200
    assert fake.calls == [["hello"]]


def test_bad_batches_are_refused_without_reaching_the_model(live_daemon):
    base, fake = live_daemon

    for payload in (
        {},
        {"input": []},
        {"input": [1, 2]},
        {"input": ["x"] * 257},
    ):
        response = requests.post(f"{base}/embeddings", json=payload, timeout=5)
        assert response.status_code == 500, payload

    assert fake.calls == []


def test_unknown_paths_are_404(live_daemon):
    base, _fake = live_daemon

    assert requests.post(f"{base}/nope", json={}, timeout=5).status_code == 404
    assert requests.get(f"{base}/nope", timeout=5).status_code == 404


def test_preload_loads_the_model_once(monkeypatch):
    loaded: list[int] = []

    def _load():
        loaded.append(1)
        return _FakeModel()

    monkeypatch.setattr(core, "_load_model", _load)
    monkeypatch.setattr(daemon, "_model", None)

    first = daemon._get_model()
    second = daemon._get_model()

    assert first is second
    assert loaded == [1]


class _RecordingBackend(core._LocalEmbeddingBackend):
    """The real strategy with the daemon transport faked at the module seams."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[list[str]] = []


def _wire(monkeypatch, backend, responses):
    state = {"ready": ReadyState.READY}

    monkeypatch.setattr(core, "ensure_ready", lambda *a, **k: state["ready"])

    def _request(_spec, _path, payload, *, timeout_s):
        backend.requests.append(list(payload["input"]))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(core, "request_json", _request)
    return state


def test_client_posts_one_batch_and_l2_normalises(monkeypatch):
    backend = _RecordingBackend()
    _wire(
        monkeypatch,
        backend,
        [{"data": [{"index": 1, "embedding": [0.0, 4.0]}, {"index": 0, "embedding": [3.0, 0.0]}]}],
    )

    vectors = backend.embed(["one", "two"])

    assert backend.requests == [["one", "two"]]
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


def test_client_retries_once_after_a_daemon_error(monkeypatch):
    backend = _RecordingBackend()
    _wire(
        monkeypatch,
        backend,
        [
            DaemonError("gone"),
            {"data": [{"index": 0, "embedding": [2.0, 0.0]}]},
        ],
    )

    assert backend.embed(["x"]) == [[1.0, 0.0]]
    assert backend.requests == [["x"], ["x"]]


def test_client_does_not_fall_back_while_the_daemon_is_warming(monkeypatch):
    backend = _RecordingBackend()
    state = _wire(monkeypatch, backend, [])
    state["ready"] = ReadyState.WARMING

    with pytest.raises(DaemonError, match="warming"):
        backend.embed(["x"])


def test_client_falls_back_in_process_when_no_daemon_can_start(monkeypatch):
    backend = _RecordingBackend()
    state = _wire(monkeypatch, backend, [])
    state["ready"] = ReadyState.UNAVAILABLE
    fake = _FakeModel()
    monkeypatch.setattr(core, "_load_model", lambda: fake)

    vectors = backend.embed(["a", "bb"])

    assert len(vectors) == 2
    assert vectors[0] == pytest.approx([0.7071067811865476, 0.7071067811865476])
    assert vectors[1] == pytest.approx([0.8944271909999159, 0.4472135954999579])
    assert fake.closed is True
    assert backend.requests == []
    assert backend._daemon_usable is False


def test_empty_input_short_circuits(monkeypatch):
    backend = _RecordingBackend()
    monkeypatch.setattr(core, "request_json", lambda *a, **k: pytest.fail("no call"))

    assert backend.embed([]) == []


def test_malformed_vector_batch_is_an_error(monkeypatch):
    backend = _RecordingBackend()
    _wire(monkeypatch, backend, [{"data": []}, {"data": []}])

    with pytest.raises(DaemonError, match="malformed"):
        backend.embed(["x"])


def test_partial_vector_entry_is_an_error(monkeypatch):
    backend = _RecordingBackend()
    _wire(monkeypatch, backend, [{"data": [{"index": 0}]}, {"data": [{"index": 0}]}])

    with pytest.raises(DaemonError, match="without index/embedding"):
        backend.embed(["x"])
