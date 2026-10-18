"""Local-model prefetch service (no network, no real weights)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from server.service import model_downloads_service as service

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _spec(root: Path, *, fallback: str | None = None) -> dict:
    return {
        "timeout_seconds": 5,
        "chunk_bytes": 4,
        "models": {
            "TEST": {
                "label": "test model",
                "files": (
                    {
                        "repo_id": "example/model",
                        "revision": "main",
                        "filename": "model.gguf",
                        "target_dir": "target",
                        "fallback": fallback,
                        "size_bytes": 0,
                    },
                ),
            }
        },
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(service, "MODEL_DOWNLOADS", _spec(tmp_path))
    with service._LOCK:
        service._STATES.clear()
    yield
    with service._LOCK:
        service._STATES.clear()


def test_status_is_idle_until_the_file_exists() -> None:
    status = service.status_for("TEST")

    assert status["state"] == "idle"
    assert status["files_done"] == 0
    assert status["percent"] == 0


def test_status_reports_ready_from_the_filesystem(tmp_path: Path) -> None:
    target = tmp_path / "target" / "model.gguf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"12345")

    status = service.status_for("TEST")

    assert status["state"] == "ready"
    assert status["ready"] is True
    assert status["percent"] == 100
    assert status["downloaded_bytes"] == status["total_bytes"] == 5


def test_status_exposes_partial_progress() -> None:
    with service._LOCK:
        service._STATES["TEST"] = {
            "state": "downloading",
            "error": None,
            "downloaded_bytes": 5,
            "total_bytes": 10,
        }

    status = service.status_for("TEST")

    assert status["state"] == "downloading"
    assert status["percent"] == 50


def test_start_streams_with_progress_and_marks_ready(monkeypatch) -> None:
    done = threading.Event()

    def fake_probe(url: str, timeout: float) -> int:
        return 10

    def fake_stream(
        url: str, dest: Path, *, timeout_seconds: float, chunk_bytes: int, progress=None
    ) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"0123456789")
        if progress is not None:
            progress(4, 10)
            progress(10, 10)
        done.set()
        return 10

    monkeypatch.setattr(service, "probe_size", fake_probe)
    monkeypatch.setattr(service, "stream_download", fake_stream)

    first = service.start("TEST")
    assert first["state"] in {"downloading", "ready"}
    assert done.wait(2.0)
    for _ in range(100):
        if service.status_for("TEST")["state"] == "ready":
            break
        time.sleep(0.02)
    final = service.status_for("TEST")
    assert final["state"] == "ready"
    assert final["percent"] == 100
    assert (service.ROOT_DIR / "target" / "model.gguf").read_bytes() == b"0123456789"


def test_fallback_is_copied_without_a_network_call(tmp_path: Path, monkeypatch) -> None:
    fallback = tmp_path / "fallback.gguf"
    fallback.write_bytes(b"cached")
    monkeypatch.setattr(service, "MODEL_DOWNLOADS", _spec(tmp_path, fallback="fallback.gguf"))

    def fail_stream(*args, **kwargs):
        raise AssertionError("fallback copy must not download")

    monkeypatch.setattr(service, "stream_download", fail_stream)

    service.start("TEST")
    target = tmp_path / "target" / "model.gguf"
    for _ in range(50):
        if target.is_file():
            break
        threading.Event().wait(0.02)
    assert target.read_bytes() == b"cached"
    assert service.status_for("TEST")["state"] == "ready"


def test_unknown_family_is_rejected() -> None:
    with pytest.raises(KeyError):
        service.start("NOPE")
