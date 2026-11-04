"""Live trajectory streaming over WebSocket (one channel per session).

The per-turn trajectory ledger (``server/service/trajectory_store.py``) records
tool calls and lifecycle events for the MAIN session and every subagent child
session. ``GET /sessions/:session_id/trajectory`` already reads it back on
demand; this channel makes the read LIVE: the store resolves the
``runtime.hooks.TRAJECTORY_EVENT_PUSH`` hook after each successful upsert and
this module fans the row out to the sockets subscribed under that session id
(the agent-activity tab subscribes with the CHILD session key, so a subagent's
tool calls stream without touching the parent's channel).

Frame contract::

    {"event": "ready", "session_id": ..., "events": [...snapshot...]}
    {"event": "trajectory_event", "session_id": ..., "data": {event_id, turn_id,
     seq, ts, kind, is_error, summary, payload}}

The query is ``?session_id=...`` (Robyn's WebSocketAdapter exposes no path
params — every socket in this server routes by query). The gateway token /
login ticket gates are the shared ``WSPushChannel.serve`` handshake.
"""

from __future__ import annotations

import json

from loguru import logger
from pub.func.validator import is_safe_session_id
from robyn import WebSocketAdapter

from runtime import hooks
from server.service.trajectory_store import get_trajectory_store
from server.trigger.core import app
from server.trigger.ws.push_channel import WSPushChannel

__all__ = ["trajectory_ws_handler"]

_channel = WSPushChannel()


def _push(session_id: str, event: dict) -> None:
    """Hook implementation: enqueue one event to that session's subscribers."""
    frame = json.dumps(
        {"event": "trajectory_event", "session_id": session_id, "data": event},
        ensure_ascii=False,
        default=str,
    )
    _channel.enqueue_frame(frame, key=session_id)


def _ensure_hook_registered() -> None:
    """Register the push hook (last-writer-wins, idempotent)."""
    hooks.register(hooks.TRAJECTORY_EVENT_PUSH, _push)


# Register at import (``server/trigger/ws/__init__.py`` pulls this module in at
# boot): events recorded before the first connection then resolve the hook too,
# and a subscriber that connects later gets the snapshot from `ready`.
_ensure_hook_registered()


async def _snapshot(session_id: str) -> dict:
    """The gap-fill the ready frame carries: the newest bounded event page."""
    try:
        events = await get_trajectory_store().read(session_id, limit=100)
    except Exception:  # noqa: BLE001 - a cold/absent ledger is an empty snapshot
        logger.debug("trajectory WS: snapshot failed for {}", session_id, exc_info=True)
        events = []
    return {"session_id": session_id, "events": events}


@app.websocket("/sessions/trajectory/ws")
async def trajectory_ws_handler(websocket: WebSocketAdapter):
    query_params = getattr(websocket, "query_params", {}) or {}
    session_id = str(query_params.get("session_id", "") or "")
    if not is_safe_session_id(session_id):
        logger.warning("Trajectory WS connection rejected: invalid session_id")
        await websocket.close()
        return
    await _channel.serve(
        websocket,
        ensure_registered=_ensure_hook_registered,
        handler_label="Trajectory WS",
        client_label="Trajectory WS",
        channel_key=session_id,
        ready_factory=lambda: _snapshot(session_id),
    )
