"""Browser voice input's server half: a WAV → STT-daemon proxy.

The FunASR SenseVoice daemon
(``skills/builtin/core/speech_to_text/scripts/server.py``) already owns
transcription — one warm model, loopback HTTP JSON, a file-path contract. It
binds ``127.0.0.1`` only, speaks no CORS/auth headers, and accepts only a local
absolute path, so a browser can neither reach it nor hand it a Blob. This
service is the process-local bridge: it parks an uploaded WAV under
``TEMP_DIR``, drives the SAME daemon lifecycle the skill's own client uses
(probe → spawn → wait, shared ``.stt_daemon.pid`` semantics via the skill's
public aliases), and deletes the file afterwards.

Availability follows the real configuration surface: the model weights under
``MODELS_DIR/STT_model/model_weight``. The daemon itself cold-starts lazily on
the first transcribe request (never at import), so ``daemon_ready = False``
merely means "not warm yet" — the route answers that state with a machine
readable ``warming`` signal instead of treating it as misconfiguration.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import requests
from loguru import logger

from config import MODELS_DIR, TEMP_DIR
from skills.builtin.core.speech_to_text.scripts.core import (
    DAEMON_BASE,
    HTTP_TIMEOUT,
    READY_WAIT,
    daemon_alive,
    spawn_daemon,
)

__all__ = [
    "TranscribeError",
    "TranscribeUnavailableError",
    "TranscribeWarmingError",
    "probe_status",
    "transcribe_wav",
]

_MODEL_WEIGHT_DIR = MODELS_DIR / "STT_model" / "model_weight"


class TranscribeUnavailableError(RuntimeError):
    """The STT model weights are not provisioned — nothing to transcribe with."""


class TranscribeWarmingError(RuntimeError):
    """The daemon is (still) cold-starting; retry shortly."""


class TranscribeError(RuntimeError):
    """The daemon answered, but transcription failed (or it answered garbage)."""


def probe_status() -> dict[str, bool]:
    """The one availability snapshot the mic button renders from.

    ``model_dir_present`` is the configuration truth (weights on disk);
    ``daemon_ready`` is informational — a cold daemon spawns on first use.
    """
    model_dir_present = _MODEL_WEIGHT_DIR.is_dir()
    return {"model_dir_present": model_dir_present, "daemon_ready": daemon_alive()}


def _wait_for_daemon() -> None:
    """Ensure the daemon is up and ready, spawning it once if needed.

    Mirrors the skill client's cold-start flow: probe, spawn on the first miss,
    poll up to ``READY_WAIT``. A daemon whose pid is recorded but whose model is
    still loading answers ``ready: false`` — that is a warming state, not a
    spawn target (``spawn_daemon`` no-ops on a live pid).
    """
    if daemon_alive():
        return
    spawn_daemon()
    deadline = time.monotonic() + READY_WAIT
    while time.monotonic() < deadline:
        if daemon_alive():
            return
        time.sleep(0.5)
    raise TranscribeWarmingError("STT daemon is cold-starting; retry shortly")


def _sanitize_detail(detail: str) -> str:
    """Keep the daemon's own diagnosis but never serve our temp path back."""
    return (
        str(detail or "").replace(str(TEMP_DIR), "<temp>").strip()[:400] or "transcription failed"
    )


def transcribe_wav(data: bytes, ext: str) -> dict[str, Any]:
    """Transcribe one uploaded audio payload through the STT daemon.

    :param data: Raw audio bytes (the browser sends 16 kHz mono PCM WAV).
    :param ext: File extension for the temp parking lot (from the request's
        content type, same map ``/audio/upload`` uses).
    :returns: ``{"text": ..., "elapsed_ms": ...}``.
    """
    if not _MODEL_WEIGHT_DIR.is_dir():
        # The configuration gate, checked first: without weights there is
        # nothing to spawn, and a live pid cannot have loaded a missing model.
        raise TranscribeUnavailableError("STT model weights are not provisioned")
    started = time.monotonic()
    temp_path = TEMP_DIR / f"voice-{uuid.uuid4().hex}{ext}"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        temp_path.write_bytes(data)
        _wait_for_daemon()
        try:
            resp = requests.post(
                f"{DAEMON_BASE}/transcribe",
                json={"audio_path": temp_path.as_posix()},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as error:
            raise TranscribeError(f"STT daemon unreachable: {error}") from error
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", f"HTTP {resp.status_code}")
            except ValueError:
                detail = f"HTTP {resp.status_code}"
            raise TranscribeError(_sanitize_detail(detail))
        try:
            text = str(resp.json().get("text", ""))
        except ValueError as error:
            raise TranscribeError("STT daemon returned a non-JSON body") from error
    finally:
        temp_path.unlink(missing_ok=True)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("Voice transcribed: chars={} elapsed_ms={}", len(text), elapsed_ms)
    return {"text": text, "elapsed_ms": elapsed_ms}
