import asyncio
import json
import threading
from collections import deque
from typing import Any
from loguru import logger
from server.trigger.core import app
from robyn import WebSocketDisconnect, WebSocketAdapter

# ---------------------------------------------------------------------------
# Real-time log streaming over WebSocket.
#
# Design decision (send mechanism):
#   loguru's sinks are configured with ``enqueue=True`` (see logs/logger.py),
#   so the sink callback below runs on loguru's *background* thread, NOT on
#   the asyncio event loop. We therefore never ``await websocket.send_text``
#   from the sink. Instead:
#
#     1. Each connected websocket owns a bounded ``deque`` of pending frames
#        (max ~2000). The sink only appends to that deque under a lock — it
#        never touches asyncio primitives, so it can never block the loguru
#        thread.
#
#     2. The handler coroutine spawns a dedicated *sender* task via
#        ``asyncio.create_task`` that drains the per-websocket deque and
#        performs the actual ``await websocket.send_text`` on the event loop
#        thread. This keeps all sends on the loop thread while the sink stays
#        thread-safe and non-blocking.
#
#     3. A separate receive loop keeps the connection alive and detects
#        disconnects; when it exits (disconnect/close), the sender task is
#        cancelled and the websocket is unregistered.
#
#   The shared subscriber set is guarded by a ``threading.Lock`` because the
#   sink (background thread) and the handler (event loop thread) both mutate
#   it.
# ---------------------------------------------------------------------------

# Max pending frames per websocket before we start dropping the oldest.
_MAX_PENDING = 2000

# Shared set of connected websockets + the lock guarding it.
_subscribers: set[WebSocketAdapter] = set()
_subscribers_lock = threading.Lock()

# Per-websocket bounded deque of serialized JSON frames.
_pending: dict[WebSocketAdapter, deque[str]] = {}

# Idempotency guard: the sink must only be registered once even if this module
# is re-imported on server reload.
_sink_registered = False
_sink_lock = threading.Lock()


def _serialize_record(record: dict[str, Any]) -> str:
    """Serialize a loguru record into the wire JSON frame."""
    data = {
        "timestamp": str(record["time"]),
        "level": record["level"].name,
        "name": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }
    return json.dumps({"event": "log", "data": data}, ensure_ascii=False)


def _log_sink(message):
    """loguru sink: push a serialized frame to every connected websocket.

    Runs on loguru's background thread. Only touches the thread-safe deque
    and the lock-guarded subscriber set — never awaits asyncio primitives.
    """
    try:
        record = message.record
        frame = _serialize_record(record)
    except Exception:
        # Never let a serialization failure break the logging pipeline.
        return

    with _subscribers_lock:
        for ws in list(_subscribers):
            queue = _pending.get(ws)
            if queue is None:
                continue
            # Bounded queue: drop the oldest frame when full so a slow
            # consumer cannot cause unbounded memory growth.
            if len(queue) >= _MAX_PENDING:
                queue.popleft()
            queue.append(frame)


def _ensure_sink_registered() -> None:
    """Register the streaming sink exactly once (idempotent on reload)."""
    global _sink_registered
    with _sink_lock:
        if _sink_registered:
            return
        # format="" disables loguru's default formatting; we build the frame
        # ourselves in _log_sink. enqueue=True keeps the sink off the loop.
        logger.add(_log_sink, format="", enqueue=True)
        _sink_registered = True


async def _sender(websocket: WebSocketAdapter, queue: deque[str]) -> None:
    """Drain the per-websocket deque and send frames on the event loop."""
    while True:
        # Wait for at least one frame to be available.
        while not queue:
            await asyncio.sleep(0.05)
        while queue:
            frame = queue.popleft()
            try:
                await websocket.send_text(frame)
            except Exception:
                return


@app.websocket("/logs/ws")
async def logs_ws_handler(websocket: WebSocketAdapter):
    logger.info(f"Logs WebSocket handler started: websocket_id={websocket.id}")
    _ensure_sink_registered()

    queue: deque[str] = deque()
    with _subscribers_lock:
        _subscribers.add(websocket)
        _pending[websocket] = queue

    sender_task = asyncio.create_task(_sender(websocket, queue))

    try:
        # Welcome / backfill frame.
        await websocket.send_text(json.dumps({"event": "ready"}, ensure_ascii=False))

        # Receive loop: keeps the connection alive and detects disconnect.
        while True:
            try:
                await websocket.receive_text()
            except Exception:
                break
    except (WebSocketDisconnect, ConnectionResetError, Exception) as e:
        logger.warning(f"Logs WS client {websocket.id} disconnected: {e}")
    finally:
        sender_task.cancel()
        with _subscribers_lock:
            _subscribers.discard(websocket)
            _pending.pop(websocket, None)
        logger.info(f"Logs WS client {websocket.id} unregistered")
