"""Code embedder download/status service (no network, no 612 MB weights)."""

from __future__ import annotations

import threading

import pytest

from server.service import code_embedder_service as service

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _reset_state():
    with service._LOCK:
        service._STATE.update(state="idle", error=None)
    yield
    with service._LOCK:
        service._STATE.update(state="idle", error=None)


def test_status_reports_off_until_provisioned(monkeypatch) -> None:
    monkeypatch.setattr(service, "code_embedder_available", lambda: False)
    monkeypatch.setattr(service, "code_embedder_enabled", lambda: False)

    status = service.status()

    assert status["state"] == "idle"
    assert status["files_ready"] is False
    assert status["enabled"] is False
    assert status["profile"] == "jina-embeddings-v2-base-code"
    assert status["size_bytes"] == 641517466


def test_status_distinguishes_downloaded_from_enabled(monkeypatch) -> None:
    monkeypatch.setattr(service, "code_embedder_available", lambda: True)
    monkeypatch.setattr(service, "code_embedder_enabled", lambda: False)

    assert service.status()["state"] == "downloaded"

    monkeypatch.setattr(service, "code_embedder_enabled", lambda: True)
    assert service.status()["state"] == "ready"
    assert service.status()["enabled"] is True


def test_status_reports_last_error(monkeypatch) -> None:
    monkeypatch.setattr(service, "code_embedder_available", lambda: False)
    monkeypatch.setattr(service, "code_embedder_enabled", lambda: False)
    with service._LOCK:
        service._STATE.update(state="error", error="network down")

    status = service.status()

    assert status["state"] == "error"
    assert status["error"] == "network down"


def test_download_runs_in_background_and_enables(monkeypatch) -> None:
    done = threading.Event()
    available = {"value": False}
    enabled = {"value": False}

    def fake_download() -> None:
        available["value"] = True

    def fake_enable() -> None:
        enabled["value"] = True
        done.set()

    monkeypatch.setattr(service, "download_code_embedder", fake_download)
    monkeypatch.setattr(service, "enable_code_embedder", fake_enable)
    monkeypatch.setattr(service, "code_embedder_available", lambda: available["value"])
    monkeypatch.setattr(service, "code_embedder_enabled", lambda: enabled["value"])

    first = service.start_download()
    assert first["state"] in {"downloading", "ready"}
    assert done.wait(2.0)
    final = service.status()
    assert final["state"] == "ready"
    assert final["enabled"] is True


def test_already_downloaded_enables_without_a_thread(monkeypatch) -> None:
    available = {"value": True}
    enabled = {"value": False}

    def fake_enable() -> None:
        enabled["value"] = True

    monkeypatch.setattr(service, "code_embedder_available", lambda: available["value"])
    monkeypatch.setattr(service, "code_embedder_enabled", lambda: enabled["value"])
    monkeypatch.setattr(service, "enable_code_embedder", fake_enable)

    assert service.start_download()["state"] == "ready"
    assert enabled["value"] is True


def test_disable_only_removes_the_marker(monkeypatch) -> None:
    enabled = {"value": True}
    monkeypatch.setattr(service, "code_embedder_enabled", lambda: enabled["value"])
    monkeypatch.setattr(service, "code_embedder_available", lambda: True)

    def fake_disable() -> None:
        enabled["value"] = False

    monkeypatch.setattr(service, "_disable", fake_disable)

    assert service.disable()["enabled"] is False
    assert service.status()["state"] == "downloaded"
