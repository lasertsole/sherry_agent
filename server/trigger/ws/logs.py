import json
import threading
from typing import Any
from loguru import logger
from server.trigger.core import app
from server.trigger.ws.push_channel import WSPushChannel
from robyn import WebSocketAdapter

# ---------------------------------------------------------------------------
# Real-time log streaming over WebSocket.
#
# The push machinery (subscriber set, bounded per-websocket deques, sender
# task, handler skeleton) is shared with subagent_ws.py via WSPushChannel
# (audit 2.1.4). What remains here is logs-specific: the frame serializer and
# the loguru sink registration.
#
# Design decision (send mechanism):
#   loguru's sinks are configured with ``enqueue=True`` (see logs/logger.py),
#   so the sink callback below runs on loguru's *background* thread, NOT on
#   the asyncio event loop. We therefore never ``await websocket.send_text``
#   from the sink. The sink only appends a pre-serialized frame to each
#   subscriber's bounded deque under a lock (never touches asyncio
#   primitives, so it can never block the loguru thread); the channel's
#   sender task performs the actual ``await send_text`` on the loop thread.
# ---------------------------------------------------------------------------

# Idempotency guard: the sink must only be registered once even if this module
# is re-imported on server reload.
_sink_registered = False
_sink_lock = threading.Lock()

_channel = WSPushChannel()


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

    Runs on loguru's background thread. Only touches the thread-safe frame
    serialization and the channel's lock-guarded deques — never awaits
    asyncio primitives. Never lets a serialization failure break the logging
    pipeline.
    """
    try:
        record = message.record
        frame = _serialize_record(record)
    except Exception:
        return
    _channel.enqueue_frame(frame)


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


@app.websocket("/logs/ws")
async def logs_ws_handler(websocket: WebSocketAdapter):
    await _channel.serve(
        websocket,
        ensure_registered=_ensure_sink_registered,
        handler_label="Logs WebSocket",
        client_label="Logs WS",
    )
