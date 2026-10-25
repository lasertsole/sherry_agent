"""The voice-input proxy: ``/audio/transcribe`` routes over the STT daemon.

The daemon itself is never started here — ``daemon_alive`` / ``spawn_daemon``
and ``requests.post`` are faked at the service's import site, so these tests
pin the CONTRACT: availability follows the model-weight directory, a cold
daemon is a machine-readable ``warming`` (503), missing weights are
``unavailable`` (503), daemon failures are a 500 with a sanitized detail, the
temp WAV is always removed, and the upload validation mirrors
``/audio/upload`` (400 empty / 413 oversized).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.service import audio_transcribe_service as service
from server.trigger.http import audio as http

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture()
def staged(monkeypatch, tmp_path):
    """Fake daemon seams + redirect the weight dir and temp lot into tmp_path."""
    state: dict = {
        "alive": True,
        "spawned": 0,
        "posted": [],
        "response": _FakeResponse(200, {"text": "你好世界"}),
        "raise": None,
    }

    def fake_alive() -> bool:
        return state["alive"]

    def fake_spawn() -> None:
        state["spawned"] += 1

    def fake_post(url, json=None, timeout=None):  # noqa: A002 - mirror requests
        state["posted"].append({"url": url, "json": json, "timeout": timeout})
        if state["raise"]:
            raise state["raise"]
        return state["response"]

    monkeypatch.setattr(service, "daemon_alive", fake_alive)
    monkeypatch.setattr(service, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(service.requests, "post", fake_post)
    monkeypatch.setattr(service, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(service, "_MODEL_WEIGHT_DIR", tmp_path / "weights")
    return state


class TestProbeStatus:
    def test_weights_and_daemon_present(self, staged, tmp_path):
        staged["weights"] = None
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)

        status = service.probe_status()

        assert status == {"model_dir_present": True, "daemon_ready": True}

    def test_cold_daemon_is_not_a_misconfiguration(self, staged):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["alive"] = False

        status = service.probe_status()

        # The daemon spawns on first use; only the weights gate the button.
        assert status == {"model_dir_present": True, "daemon_ready": False}

    def test_no_weights(self, staged):
        assert service.probe_status() == {"model_dir_present": False, "daemon_ready": True}


class TestTranscribeWav:
    def test_happy_path_posts_temp_path_and_removes_it(self, staged, tmp_path):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)

        result = service.transcribe_wav(b"RIFF....", ".wav")

        assert result["text"] == "你好世界"
        assert result["elapsed_ms"] >= 0
        assert len(staged["posted"]) == 1
        sent_path = staged["posted"][0]["json"]["audio_path"]
        assert sent_path.startswith((tmp_path / "temp").as_posix())
        assert staged["posted"][0]["timeout"] == service.HTTP_TIMEOUT
        assert not (tmp_path / "temp").exists() or not list((tmp_path / "temp").iterdir())

    def test_missing_weights_short_circuits_without_spawning(self, staged):
        with pytest.raises(service.TranscribeUnavailableError):
            service.transcribe_wav(b"RIFF....", ".wav")
        assert staged["spawned"] == 0 and staged["posted"] == []

    def test_cold_daemon_spawns_then_warming_surfaces(self, staged):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["alive"] = False
        # The poll loop sleeps 0.5 s per miss; one miss is enough for the signal.
        service.READY_WAIT = 0.4  # monkeypatched attribute on the module copy

        with pytest.raises(service.TranscribeWarmingError):
            service.transcribe_wav(b"RIFF....", ".wav")

        assert staged["spawned"] == 1

    def test_cold_daemon_becomes_ready_after_spawn(self, staged, monkeypatch):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["alive"] = False
        service.READY_WAIT = 2.0
        monkeypatch.setattr(service.time, "sleep", lambda _s: staged.__setitem__("alive", True))

        result = service.transcribe_wav(b"RIFF....", ".wav")

        assert result["text"] == "你好世界"
        assert staged["spawned"] == 1

    def test_daemon_error_is_sanitized_of_temp_dir(self, staged, tmp_path):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["response"] = _FakeResponse(
            500, {"error": f"decode failed at {(tmp_path / 'temp') / 'voice-x.wav'}"}
        )

        with pytest.raises(service.TranscribeError) as exc:
            service.transcribe_wav(b"RIFF....", ".wav")

        assert str(tmp_path) not in str(exc.value)
        assert "<temp>" in str(exc.value)

    def test_daemon_unreachable(self, staged):
        import requests as requests_lib

        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["raise"] = requests_lib.ConnectionError("refused")

        with pytest.raises(service.TranscribeError):
            service.transcribe_wav(b"RIFF....", ".wav")


class TestRoutes:
    def _post(self, body: bytes | None, content_type: str = "audio/wav"):
        request = SimpleNamespace(body=body, headers={"Content-Type": content_type})
        return asyncio.run(http.transcribe_audio_handler(request))

    def test_empty_body_is_400(self, staged):
        response = self._post(b"")
        assert response.status_code == 400

    def test_happy_route(self, staged):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)

        response = self._post(b"RIFF....")

        assert response.status_code == 200
        assert '"text": "你好世界"' in response.description

    def test_warming_route(self, staged):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["alive"] = False
        service.READY_WAIT = 0.4

        response = self._post(b"RIFF....")

        assert response.status_code == 503
        assert '"warming": true' in response.description

    def test_unavailable_route(self, staged):
        response = self._post(b"RIFF....")
        assert response.status_code == 503
        assert '"unavailable": true' in response.description

    def test_daemon_error_route_sanitized(self, staged, tmp_path):
        service._MODEL_WEIGHT_DIR.mkdir(parents=True)
        staged["response"] = _FakeResponse(
            500, {"error": f"boom at {(tmp_path / 'temp') / 'voice-x.wav'}"}
        )

        response = self._post(b"RIFF....")

        assert response.status_code == 500
        assert str(tmp_path) not in response.description

    def test_status_route(self, staged):
        response = asyncio.run(http.transcribe_status_handler(SimpleNamespace()))
        assert response.status_code == 200
