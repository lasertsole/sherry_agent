"""Real-time flow progress: the ``taskflow_updated`` frame the panel listens to.

Same shape and same best-effort contract as the todolist's ``todo_updated`` push
(``agent/tools/todolist/service.py::_push_todo_update``): the payload is built from
persisted state, sent to the session's websocket when one is bound, and every
failure is logged and swallowed so a dead socket can never break a tool call.

The frame content is also what ``taskflow_refresh`` answers with, so a reconnecting
client recovers the panel with one round trip and no separate endpoint:

    {"event": "taskflow_updated", "session_id": …, "content": {
        "flows": [{"flow_id", "status", "description", "current_wave",
                   "totals": {"done", "total", "by_status"}, "waves": [...]}],
        "totals": {"flows", "done", "total", "current_wave"}}}
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from runtime.session.relation_register import relation_register

from .registry import store_sqlite
from .waves import STATUS_ORDER, compute_waves, steps_of

__all__ = ["flow_progress", "progress_payload", "push_taskflow_progress", "TASKFLOW_UPDATED_EVENT"]

#: The frame event name (client-side `ws:taskflow_updated` mitt event).
TASKFLOW_UPDATED_EVENT = "taskflow_updated"

#: Description text longer than this is clipped on the wire.
_DESCRIPTION_WIDTH = 200


def flow_progress(flow: dict[str, Any]) -> dict[str, Any]:
    """The wire shape of ONE flow: its waves, totals and where it stands."""
    steps = steps_of(flow)
    waves = compute_waves(steps)
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
        # The first wave that still has unfinished work; 0 = nothing left (or no steps).
        "current_wave": next(
            (wave["index"] for wave in waves if wave["done"] < wave["total"]),
            0,
        ),
        "by_status": by_status,
        "waves": waves,
    }


def progress_payload(session_id: str) -> dict[str, Any]:
    """The content block for a session's active flows (waves + an aggregate).

    Reading persisted state is deliberate: the panel shows what the flow IS, not
    what an in-flight tool call intended.
    """
    flows = [flow_progress(flow) for flow in store_sqlite.get_active_flows_sync(session_id)]
    total = sum(flow["total"] for flow in flows)
    done = sum(flow["done"] for flow in flows)
    first_open = next((flow for flow in flows if flow["done"] < flow["total"]), None)
    return {
        "flows": flows,
        "totals": {
            "flows": len(flows),
            "total": total,
            "done": done,
            "current_wave": first_open["current_wave"] if first_open else 0,
            "waves": sum(len(flow["waves"]) for flow in flows),
        },
    }


async def push_taskflow_progress(session_id: str) -> None:
    """Best-effort ``taskflow_updated`` push to the session's websocket.

    Called after every flow mutation; a missing websocket (no client attached, the
    session never opened a socket) is the normal quiet case, and any failure is
    logged and swallowed so the tool path is never broken by a send error.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return
    try:
        websocket = relation_register.get_websocket_by_session_id(session_id)
        if websocket is None:
            return
        payload = {
            "event": TASKFLOW_UPDATED_EVENT,
            "session_id": session_id,
            "content": progress_payload(session_id),
        }
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — a push failure must never surface
        logger.warning("Failed to push taskflow_updated for session {}: {}", session_id, e)
