"""Real-time sub-agent run streaming over WebSocket.

The client's "后台任务" (background tasks) tab shows a live list of the
sub-agents spawned by a session. This module provides an event-driven push
channel: it registers sub-agent lifecycle hooks (spawned / ended) so that
whenever the sub-agent system creates or finishes a run, the serialized run
record (public-fields-only) is broadcast to every connected WebSocket
client. The HTTP ``GET /subagents/runs`` endpoint provides the initial /
gap-fill snapshot; this push channel keeps the UI live between fetches.

The push machinery (subscriber set, bounded per-websocket deques, sender
task, handler skeleton) is shared with logs.py via WSPushChannel (audit
2.1.4); run-record serialization is shared with server/trigger/http/subagent.py
via server.trigger.subagent_serialize (audit 2.1.5).

The hook registrars are idempotent and the subscriber set is guarded by a
``threading.Lock`` because hooks (event-loop / task threads) and the handler
(event-loop thread) both mutate the shared state.
"""

import json
import threading

from loguru import logger

from server.trigger.core import app
from server.trigger.subagent_serialize import PUBLIC_FIELDS as _PUBLIC_FIELDS
from server.trigger.subagent_serialize import serialize_run as _serialize_run
from server.trigger.ws.push_channel import WSPushChannel
from robyn import WebSocketAdapter

from agent.tools.subagent.hooks import register_spawned_hook, register_ended_hook

# Audit 2.1.5: _PUBLIC_FIELDS / _serialize_run are imported from the shared
# server.trigger.subagent_serialize module (aliases keep call sites unchanged).

# Idempotency guard: hooks must only be registered once even if this module is
# re-imported on server reload.
_hooks_registered = False
_hooks_lock = threading.Lock()

_channel = WSPushChannel()


def _frame(event: str, run) -> str:
    """Build the wire JSON frame for a run lifecycle event."""
    data = _serialize_run(run)
    return json.dumps({"event": event, "data": data}, ensure_ascii=False)


def _broadcast(event_name: str, run) -> None:
    """Enqueue a serialized run frame to every connected websocket.

    Safe to call from any thread/async context; never awaits asyncio. Never
    lets a serialization failure break the lifecycle flow.
    """
    try:
        frame = _frame(event_name, run)
    except Exception:
        logger.debug("Subagent WS serialization failed for run: {}", getattr(run, "run_id", "?"))
        return

    _channel.enqueue_frame(frame)


async def _on_spawned(run) -> None:
    """Broadcast a spawned event (wire event: ``subagent_spawned``)."""
    _broadcast("subagent_spawned", run)


async def _on_ended(run) -> None:
    """Broadcast an ended event (wire event: ``subagent_ended``)."""
    _broadcast("subagent_ended", run)


def _ensure_hooks_registered() -> None:
    """Register the spawn / ended hooks exactly once (idempotent on reload)."""
    global _hooks_registered
    with _hooks_lock:
        if _hooks_registered:
            return
        register_spawned_hook(_on_spawned)
        register_ended_hook(_on_ended)
        _hooks_registered = True
        logger.info("Subagent WS hooks registered (spawned / ended)")


@app.websocket("/subagents/ws")
async def subagents_ws_handler(websocket: WebSocketAdapter):
    await _channel.serve(
        websocket,
        ensure_registered=_ensure_hooks_registered,
        handler_label="Subagent WS",
        client_label="Subagent WS",
    )
