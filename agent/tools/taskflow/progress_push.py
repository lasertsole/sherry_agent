"""Real-time flow progress: the ``taskflow_updated`` frame the panel listens to.

Same shape and same best-effort contract as the todolist's ``todo_updated`` push
(``agent/tools/todolist/service.py::_push_todo_update``): the payload is built from
persisted state, sent to the session's websocket when one is bound, and every
failure is logged and swallowed so a dead socket can never break a tool call.

The payload reports a FLAT step list per flow. TaskFlow schedules steps
individually (a step turns ``ready`` when its OWN dependencies are done), so the
wire shape must not imply "round N must finish before round N+1" — the removed
longest-path wave grouping did exactly that, and the scheduler never used it.

The frame content is also what ``taskflow_refresh`` answers with, so a reconnecting
client recovers the panel with one round trip and no separate endpoint:

    {"event": "taskflow_updated", "session_id": …, "content": {
        "flows": [{"flow_id", "status", "description",
                   "totals": {"done", "total", "by_status"}, "steps": [...]}],
        "totals": {"flows", "done", "total"}}}
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from runtime.session.relation_register import relation_register
from runtime.session.ws_push import broadcast_to_session

from .registry import store_sqlite

__all__ = [
    "flow_progress",
    "maybe_prune_terminal_flows",
    "progress_payload",
    "push_taskflow_progress",
    "TASKFLOW_UPDATED_EVENT",
]

#: Per-session monotonic timestamp of the last background prune.
_LAST_PRUNE: dict[str, float] = {}

#: The frame event name (client-side `ws:taskflow_updated` mitt event).
TASKFLOW_UPDATED_EVENT = "taskflow_updated"

#: Description text longer than this is clipped on the wire.
_DESCRIPTION_WIDTH = 200

#: Task text longer than this is clipped on the wire (the panel is an overview).
_TASK_WIDTH = 200

#: Statuses the wire shape always carries (zero-filled), in display order.
STATUS_ORDER: tuple[str, ...] = (
    "done",
    "dispatched",
    "ready",
    "blocked",
    "failed",
    "skipped",
    "cancelled",
)


def steps_of(flow: dict[str, Any]) -> list[dict]:
    """The step list inside a flow row (``state.steps``, defensive)."""
    state = flow.get("state") or {}
    steps = state.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _wire_step(step: dict) -> dict[str, str]:
    """One flat step row as the panel consumes it."""
    return {
        "step_id": str(step.get("step_id") or ""),
        "task": str(step.get("task") or "")[:_TASK_WIDTH],
        "status": str(step.get("status") or "ready"),
    }


def flow_progress(flow: dict[str, Any]) -> dict[str, Any]:
    """The wire shape of ONE flow: its flat steps and status totals."""
    steps = steps_of(flow)
    by_status = {status: 0 for status in STATUS_ORDER}
    for step in steps:
        status = str(step.get("status") or "ready")
        by_status[status] = by_status.get(status, 0) + 1
    done = by_status["done"]
    state = flow.get("state") or {}
    return {
        "flow_id": str(flow.get("flow_id") or ""),
        "status": str(flow.get("status") or ""),
        "description": str(state.get("description") or "")[:_DESCRIPTION_WIDTH],
        "total": len(steps),
        "done": done,
        "by_status": by_status,
        "steps": [_wire_step(step) for step in steps],
    }


def progress_payload(session_id: str) -> dict[str, Any]:
    """The content block for a session's active flows (flat steps + an aggregate).

    Reading persisted state is deliberate: the panel shows what the flow IS, not
    what an in-flight tool call intended.
    """
    flows = [flow_progress(flow) for flow in store_sqlite.get_active_flows_sync(session_id)]
    total = sum(flow["total"] for flow in flows)
    done = sum(flow["done"] for flow in flows)
    return {
        "flows": flows,
        "totals": {"flows": len(flows), "total": total, "done": done},
    }


async def maybe_prune_terminal_flows(session_id: str) -> None:
    """Best-effort, debounced prune of old terminal/superseded flow rows.

    Called after every mutation (including the quiet no-socket case). Active
    rows are never touched; failures are swallowed so a maintenance sweep can
    never break a tool call.
    """
    from config.features import TASKFLOW_INFRA

    session_id = (session_id or "").strip()
    if not session_id or int(TASKFLOW_INFRA["max_total_flows_per_session"]) <= 0:
        return
    now = time.monotonic()
    last = _LAST_PRUNE.get(session_id)
    debounce = float(TASKFLOW_INFRA["prune_debounce_seconds"])
    if last is not None and now - last < debounce:
        return
    _LAST_PRUNE[session_id] = now
    try:
        deleted = await store_sqlite.prune_session_flows(session_id)
        if deleted:
            logger.info("TaskFlow prune removed {} old terminal row(s) for {}", deleted, session_id)
    except Exception as exc:  # noqa: BLE001 - maintenance only
        logger.debug("TaskFlow prune skipped for {}: {}", session_id, exc)


async def push_taskflow_progress(session_id: str) -> None:
    """Best-effort ``taskflow_updated`` push to the session's websocket.

    Called after every flow mutation; a missing websocket (no client attached, the
    session never opened a socket) is the normal quiet case, and any failure is
    logged and swallowed so the tool path is never broken by a send error.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return
    # Maintenance runs even with no viewer (the early return below is about the
    # frame, not the store). It is awaited rather than backgrounded: a fire-and-
    # forget task outliving a request/test loop is exactly the aiosqlite
    # "event loop is closed" noise we do not want. The debounce keeps the cost
    # to one count/delete per session per interval.
    await maybe_prune_terminal_flows(session_id)
    try:
        # Quiet no-op before the (store-reading) payload build: with no live
        # viewer there is nothing to fan out to. A socket that connects between
        # this check and the send simply misses this frame — it gets the same
        # payload from its own ``taskflow_refresh`` snapshot on connect.
        if not relation_register.get_websockets_by_session_id(session_id):
            return
        payload = {
            "event": TASKFLOW_UPDATED_EVENT,
            "session_id": session_id,
            "content": progress_payload(session_id),
        }
        await broadcast_to_session(session_id, payload, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — a push failure must never surface
        logger.warning("Failed to push taskflow_updated for session {}: {}", session_id, e)
