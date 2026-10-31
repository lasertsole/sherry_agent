"""Best-effort WebSocket fan-out for model download progress.

Both download workers run in background threads while the agent sockets live on
the server's asyncio loop, so :func:`publish` captures that loop when a download
starts and schedules the broadcast with ``run_coroutine_threadsafe``. Progress
frames are throttled per ``(kind, key)``; terminal states pass ``force=True``.
A missing loop (tests, a sync CLI) is the quiet no-op case, and no failure here
may ever break the download it describes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from config.features import MODEL_DOWNLOADS
from loguru import logger

from runtime.session.ws_push import broadcast_to_all

#: The WS event name, forwarded by the client as `ws:model_download_updated`.
EVENT_NAME = "model_download_updated"

#: `content.kind` values: the five Model Config local families vs the code embedder.
KIND_LOCAL = "local"
KIND_CODE_EMBEDDER = "code_embedder"

_LOCK = threading.RLock()
_LOOP: asyncio.AbstractEventLoop | None = None
_LAST_PUSH: dict[tuple[str, str], float] = {}


def capture_loop() -> None:
    """Remember the server loop a download worker should schedule onto.

    Called from the async HTTP start handler (through the service). A caller
    with no running loop (a unit test, a CLI import) leaves the previous loop
    alone; :func:`publish` still fail-opens on a closed/absent one.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    global _LOOP
    with _LOCK:
        _LOOP = loop


def publish(
    kind: str,
    key: str,
    builder: Callable[[], dict[str, Any]],
    *,
    force: bool = False,
) -> None:
    """Broadcast one download state frame, throttled unless ``force``.

    ``builder`` runs only when a frame will actually be sent, and only once:
    the payload is built on the calling worker thread, then serialized for
    every socket by :func:`broadcast_to_all`.
    """
    now = time.monotonic()
    slot = (kind, key)
    interval = float(MODEL_DOWNLOADS["progress_push_interval_s"])
    with _LOCK:
        loop = _LOOP
        if loop is None or loop.is_closed():
            return
        last = _LAST_PUSH.get(slot)
        if not force and last is not None and now - last < interval:
            return
        _LAST_PUSH[slot] = now

    try:
        payload = builder()
    except Exception as exc:  # noqa: BLE001 - observability must never break a download
        logger.warning("model download {}:{}: payload build failed: {}", kind, key, exc)
        return

    frame = {
        "event": EVENT_NAME,
        "content": {"kind": kind, **payload},
    }
    coroutine = broadcast_to_all(frame, ensure_ascii=False)
    try:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception as exc:  # noqa: BLE001 - a dead loop must not break the worker
        coroutine.close()
        logger.warning("model download {}:{}: WS push failed: {}", kind, key, exc)
