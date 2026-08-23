"""Real-time sub-agent run streaming over WebSocket.

The client's "后台任务" (background tasks) tab shows a live list of the
sub-agents spawned by a session. This module provides an event-driven push
channel: it registers sub-agent lifecycle hooks (spawned / ended) so that
whenever the sub-agent system creates or finishes a run, the serialized run
record (``_PUBLIC_FIELDS``-only) is broadcast to every connected WebSocket
client. The HTTP ``GET /subagents/runs`` endpoint provides the initial /
gap-fill snapshot; this push channel keeps the UI live between fetches.

The send mechanism copies ``server/trigger/ws/logs.py``: the hooks fire on the
sub-agent system's async context (not necessarily the Robyn event loop), so we
never ``await`` a send directly inside a hook. Instead each connected
websocket owns a bounded ``deque`` of pending frames; hooks only append to that
deque under a lock, and a per-connection *sender* task (drained on the loop
thread via ``asyncio.create_task``) performs the actual ``await send_text``.

The hook registrars are idempotent and the subscriber set is guarded by a
``threading.Lock`` because hooks (event-loop / task threads) and the handler
(event-loop thread) both mutate the shared state.
"""

import asyncio
import json
import threading
from collections import deque
from typing import Any

from loguru import logger

from server.trigger.core import app
from robyn import WebSocketDisconnect, WebSocketAdapter

from agent.tools.subagent.hooks import register_spawned_hook, register_ended_hook

# Fields that are safe / useful to surface to the UI. Keep in sync with
# ``server/trigger/http/subagent.py``.
_PUBLIC_FIELDS = (
    "run_id",
    "child_session_key",
    "requester_session_key",
    "task",
    "task_name",
    "label",
    "spawn_mode",
    "context_mode",
    "agent_id",
    "depth",
    "role",
    "control_scope",
    "generation",
    "swarm_group_id",
    "swarm_run_state",
    "ended_reason",
    "pause_reason",
    "execution",
    "completion",
    "delivery",
)

# Max pending frames per websocket before we start dropping the oldest.
_MAX_PENDING = 2000

# Shared set of connected websockets + the lock guarding it.
_subscribers: set[WebSocketAdapter] = set()
_subscribers_lock = threading.Lock()

# Per-websocket bounded deque of serialized JSON frames.
_pending: dict[WebSocketAdapter, deque[str]] = {}

# Idempotency guard: hooks must only be registered once even if this module is
# re-imported on server reload.
_hooks_registered = False
_hooks_lock = threading.Lock()


def _serialize_run(run) -> dict[str, Any]:
    """Convert a SubagentRunRecord into a JSON-serializable dict with only public fields."""
    # model_dump(..., mode="json") recursively converts nested pydantic models
    # (execution / completion / delivery) and enums into plain JSON-safe values
    # that can be handed directly to :func:`json.dumps`.
    return run.model_dump(include=set(_PUBLIC_FIELDS), mode="json")


def _frame(event: str, run) -> str:
    """Build the wire JSON frame for a run lifecycle event."""
    data = _serialize_run(run)
    return json.dumps({"event": event, "data": data}, ensure_ascii=False)


def _broadcast(event_name: str, run) -> None:
    """Enqueue a serialized run frame to every connected websocket.

    Safe to call from any thread/async context: only touches the guarded
    subscriber set and the per-websocket deque; never awaits asyncio.
    """
    try:
        frame = _frame(event_name, run)
    except Exception:
        # Never let a serialization failure break the lifecycle flow.
        logger.debug("Subagent WS serialization failed for run: {}", getattr(run, "run_id", "?"))
        return

    with _subscribers_lock:
        for ws in list(_subscribers):
            queue = _pending.get(ws)
            if queue is None:
                continue
            # Bounded queue: drop the oldest frame when full.
            if len(queue) >= _MAX_PENDING:
                queue.popleft()
            queue.append(frame)


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


async def _sender(websocket: WebSocketAdapter, queue: deque[str]) -> None:
    """Drain the per-websocket deque and send frames on the event loop."""
    while True:
        while not queue:
            await asyncio.sleep(0.05)
        while queue:
            frame = queue.popleft()
            try:
                await websocket.send_text(frame)
            except Exception:
                return


@app.websocket("/subagents/ws")
async def subagents_ws_handler(websocket: WebSocketAdapter):
    logger.info(f"Subagent WS handler started: websocket_id={websocket.id}")
    _ensure_hooks_registered()

    queue: deque[str] = deque()
    with _subscribers_lock:
        _subscribers.add(websocket)
        _pending[websocket] = queue

    sender_task = asyncio.create_task(_sender(websocket, queue))

    try:
        # Welcome frame.
        await websocket.send_text(json.dumps({"event": "ready"}, ensure_ascii=False))

        # Receive loop: keeps the connection alive and detects disconnect.
        while True:
            try:
                await websocket.receive_text()
            except Exception:
                break
    except (WebSocketDisconnect, ConnectionResetError, Exception) as e:
        logger.warning(f"Subagent WS client {websocket.id} disconnected: {e}")
    finally:
        sender_task.cancel()
        with _subscribers_lock:
            _subscribers.discard(websocket)
            _pending.pop(websocket, None)
        logger.info(f"Subagent WS client {websocket.id} unregistered")
