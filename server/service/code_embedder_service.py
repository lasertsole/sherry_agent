"""Download / status service for the optional code-intel embedder.

The semantic layer is OFF by default. Merely having the ONNX/tokenizer files on
disk is not enough: ``models/embed_model/code/core.py`` requires the opt-in
marker written here after a successful download. The 模型配置 panel uses these
endpoints to start the 612 MB download in a background thread and receives live
progress as ``model_download_updated`` WebSocket frames.
"""

from __future__ import annotations

import threading
from typing import Any

from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeEmbedderProfile
from loguru import logger
from models.embed_model.code import (
    code_embedder_available,
    code_embedder_enabled,
    disable_code_embedder as _disable,
    download_code_embedder,
    enable_code_embedder,
)
from server.service import model_download_events as events

_LOCK = threading.RLock()
_STATE: dict[str, Any] = {
    "state": "idle",
    "error": None,
    "downloaded_bytes": 0,
    "total_bytes": 0,
}


def _percent(done: int, total: int) -> int:
    return min(100, max(0, int(done * 100 / total))) if total > 0 else 0


def _profile_name() -> str:
    return str(CODE_INTEL_SEMANTIC["code_intel_semantic_model"])


def _profile() -> CodeEmbedderProfile:
    profiles = CODE_INTEL_SEMANTIC["code_intel_semantic_profiles"]
    return profiles[_profile_name()]


def _publish(*, force: bool = False) -> None:
    """Push one live state frame (throttled unless a state transition)."""
    events.publish(events.KIND_CODE_EMBEDDER, _profile_name(), status, force=force)


def status() -> dict[str, Any]:
    """Report the download/enable state without touching the model files."""
    profile = _profile()
    total = int(profile["size_bytes"])
    with _LOCK:
        state = str(_STATE["state"])
        error = _STATE["error"]
        downloaded = int(_STATE.get("downloaded_bytes", 0))
        total = int(_STATE.get("total_bytes", 0)) or total
    files_ready = code_embedder_available()
    enabled = code_embedder_enabled()
    if state == "downloading":
        effective = "downloading"
    elif enabled:
        effective = "ready"
    elif files_ready:
        effective = "downloaded"
    elif error:
        effective = "error"
    else:
        effective = "idle"
    if enabled or files_ready:
        downloaded, total = total, total
    return {
        "profile": profile["name"],
        "license": profile["license"],
        "natural_languages": list(profile["natural_languages"]),
        "size_bytes": total,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "percent": _percent(downloaded, total),
        "files_ready": files_ready,
        "enabled": enabled,
        "state": effective,
        "error": error,
    }


def start_download() -> dict[str, Any]:
    """Download (if needed) and enable the code embedder in the background."""
    events.capture_loop()
    size = int(_profile()["size_bytes"])
    action = "start"
    with _LOCK:
        if _STATE["state"] == "downloading":
            action = "running"
        elif code_embedder_enabled():
            _STATE.update(
                state="ready",
                error=None,
                downloaded_bytes=size,
                total_bytes=size,
            )
            action = "ready"
        elif code_embedder_available():
            # Files already came from a manual copy / an interrupted enable:
            # enabling is instant and needs no network.
            enable_code_embedder()
            _STATE.update(
                state="ready",
                error=None,
                downloaded_bytes=size,
                total_bytes=size,
            )
            action = "ready"
        else:
            _STATE.update(
                state="downloading",
                error=None,
                downloaded_bytes=0,
                total_bytes=size,
            )
    if action == "running":
        _publish()
        return status()
    if action == "ready":
        _publish(force=True)
        return status()
    # Publish the downloading state BEFORE the worker can emit progress, so no
    # client sees bytes for a download it never saw start.
    _publish(force=True)
    thread = threading.Thread(
        target=_download_and_enable, name="code-embedder-download", daemon=True
    )
    thread.start()
    return status()


def _download_and_enable() -> None:
    size = int(_profile()["size_bytes"])

    def on_progress(done: int, total: int) -> None:
        with _LOCK:
            _STATE.update(downloaded_bytes=done, total_bytes=total)
        _publish()

    try:
        download_code_embedder(progress=on_progress)
        enable_code_embedder()
        with _LOCK:
            _STATE.update(
                state="ready",
                error=None,
                downloaded_bytes=size,
                total_bytes=size,
            )
        _publish(force=True)
        logger.info("code embedder downloaded and enabled")
    except Exception as exc:  # noqa: BLE001 - the status endpoint reports it
        logger.warning("code embedder download failed: {}", exc)
        with _LOCK:
            _STATE.update(state="error", error=str(exc))
        _publish(force=True)


def disable() -> dict[str, Any]:
    """Remove the opt-in marker; the model files stay on disk."""
    events.capture_loop()
    _disable()
    with _LOCK:
        _STATE.update(state="idle", error=None)
    _publish(force=True)
    return status()
