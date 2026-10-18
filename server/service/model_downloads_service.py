"""Background prefetch for the local models shown in the 模型配置 panel.

The runtime resolvers download lazily when a model is first built; this service
lets the panel start the same download ahead of time and watch real byte
progress. Files land in the exact model_weight paths those resolvers check, so
a finished prefetch turns the next build into a local hit. One worker per model
family; the panel polls /model-downloads/status.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from config.features import MODEL_DOWNLOADS, LocalModelFile, LocalModelSpec
from config.path import ROOT_DIR
from loguru import logger
from models.downloader import probe_size, stream_download
from server.service import model_download_events as events

_LOCK = threading.RLock()
_STATES: dict[str, dict[str, Any]] = {}


def _spec(family: str) -> LocalModelSpec | None:
    return MODEL_DOWNLOADS["models"].get(family)


def _target(file: LocalModelFile) -> Path:
    return Path(ROOT_DIR) / file["target_dir"] / file["filename"]


def _fallback(file: LocalModelFile) -> Path | None:
    return Path(ROOT_DIR) / file["fallback"] if file["fallback"] else None


def _url(file: LocalModelFile) -> str:
    return f"https://huggingface.co/{file['repo_id']}/resolve/{file['revision']}/{file['filename']}"


def _percent(done: int, total: int) -> int:
    return min(100, max(0, int(done * 100 / total))) if total > 0 else 0


def _file_status(spec: LocalModelSpec) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in spec["files"]:
        target = _target(file)
        fallback = _fallback(file)
        rows.append(
            {
                "name": file["filename"],
                "exists": target.is_file(),
                "fallback_ready": bool(fallback and fallback.is_file()),
            }
        )
    return rows


def _ready(spec: LocalModelSpec) -> bool:
    return all(_target(file).is_file() for file in spec["files"])


def status_for(family: str) -> dict[str, Any]:
    """One family's state; raises KeyError for an unknown family."""
    spec = _spec(family)
    if spec is None:
        raise KeyError(f"unknown local model family: {family}")
    files = _file_status(spec)
    file_count = len(files)
    files_done = sum(1 for row in files if row["exists"])
    ready = files_done == file_count
    with _LOCK:
        state = _STATES.get(family, {})
        phase = str(state.get("state", "idle"))
        error = state.get("error")
        downloaded = int(state.get("downloaded_bytes", 0))
        total = int(state.get("total_bytes", 0))
    if ready:
        phase = "ready"
        downloaded = total = sum(_target(f).stat().st_size for f in spec["files"])
    elif phase not in {"downloading"}:
        phase = "error" if error else "idle"
    return {
        "label": spec["label"],
        "state": phase,
        "ready": ready,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "percent": 100 if ready else _percent(downloaded, total),
        "files_done": files_done,
        "file_count": file_count,
        "files": files,
        "error": error,
    }


def status() -> dict[str, Any]:
    """All local model families (the panel picks its own group)."""
    return {"models": {family: status_for(family) for family in MODEL_DOWNLOADS["models"]}}


def _publish(family: str, *, force: bool = False) -> None:
    """Push one live state frame (throttled unless a state transition)."""
    events.publish(
        events.KIND_LOCAL,
        family,
        lambda: {"family": family, **status_for(family)},
        force=force,
    )


def start(family: str) -> dict[str, Any]:
    """Start one family's prefetch; no-op when ready or already running."""
    spec = _spec(family)
    if spec is None:
        raise KeyError(f"unknown local model family: {family}")
    events.capture_loop()
    ready = False
    already_running = False
    with _LOCK:
        if _ready(spec):
            ready = True
        elif _STATES.get(family, {}).get("state") == "downloading":
            already_running = True
        else:
            _STATES[family] = {
                "state": "downloading",
                "error": None,
                "downloaded_bytes": 0,
                "total_bytes": 0,
            }
    if ready:
        _publish(family, force=True)
        return status_for(family)
    if already_running:
        _publish(family)
        return status_for(family)
    # Publish the downloading state BEFORE the worker can emit progress, so no
    # client sees bytes for a download it never saw start.
    _publish(family, force=True)
    thread = threading.Thread(
        target=_run, args=(family, spec), name=f"model-download-{family}", daemon=True
    )
    thread.start()
    return status_for(family)


def _run(family: str, spec: LocalModelSpec) -> None:
    files = spec["files"]
    targets = [_target(file) for file in files]
    fallbacks = [_fallback(file) for file in files]
    timeout = float(MODEL_DOWNLOADS["timeout_seconds"])
    chunk = int(MODEL_DOWNLOADS["chunk_bytes"])

    # Size each target up front: existing files count as done, a fallback copy
    # is a local file, and the rest get a HEAD probe (0 when unavailable).
    sizes: list[int] = []
    done_base = 0
    for file, target, fallback in zip(files, targets, fallbacks, strict=True):
        if target.is_file():
            size = target.stat().st_size
            done_base += size
        elif fallback is not None and fallback.is_file():
            size = fallback.stat().st_size
        else:
            size = int(file["size_bytes"]) or probe_size(_url(file), timeout)
        sizes.append(size)
    _update(family, total_bytes=sum(sizes), downloaded_bytes=done_base)
    _publish(family, force=True)

    try:
        for index, (file, target, fallback) in enumerate(
            zip(files, targets, fallbacks, strict=True)
        ):
            if target.is_file():
                continue
            if fallback is not None and fallback.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fallback, target)
                done_base += sizes[index]
                _update(family, downloaded_bytes=done_base, total_bytes=sum(sizes))
                _publish(family, force=True)
                continue

            def on_progress(done: int, reported_total: int, _index: int = index) -> None:
                if reported_total > 0:
                    sizes[_index] = reported_total
                _update(
                    family,
                    downloaded_bytes=done_base + done,
                    total_bytes=sum(sizes),
                )
                _publish(family)

            written = stream_download(
                _url(file),
                target,
                timeout_seconds=timeout,
                chunk_bytes=chunk,
                progress=on_progress,
            )
            sizes[index] = written
            done_base += written
            _update(family, downloaded_bytes=done_base, total_bytes=sum(sizes))
            _publish(family, force=True)

        total = sum(_target(file).stat().st_size for file in files)
        _update(
            family,
            state="ready",
            error=None,
            downloaded_bytes=total,
            total_bytes=total,
        )
        _publish(family, force=True)
        logger.info("local model {} downloaded", family)
    except Exception as exc:  # noqa: BLE001 - the status endpoint reports it
        logger.warning("local model {} download failed: {}", family, exc)
        _update(family, state="error", error=str(exc))
        _publish(family, force=True)


def _update(family: str, **changes: Any) -> None:
    with _LOCK:
        _STATES.setdefault(family, {}).update(changes)
