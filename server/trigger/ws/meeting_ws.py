"""Live meeting stream over WebSocket (phase 5, plan §6).

One channel per meeting, keyed by ``meeting_id``. Producers in the agent-side
meeting loop call the :data:`runtime.hooks.MEETING_EVENT_PUSH` hook (fail-open),
never importing this server module — same direction as the trajectory channel.

Frame contract (``event`` discriminator, free dict payload)::

    {"event": "ready", "meeting_id": ..., "messages": [...snapshot...]}
    {"event": "meeting_speaker_changed", "meeting_id": ..., "speaker_id": ..., "speaker_name": ...}
    {"event": "meeting_message_delta", "meeting_id": ..., "sequence": ..., "speaker_kind": ...,
     "speaker_id": ..., "delta": ...}
    {"event": "meeting_message_completed", "meeting_id": ..., "sequence": ..., "content": ...,
     "thinking_md": ..., "tool_calls": [...]}
    {"event": "meeting_thinking_delta", "meeting_id": ..., "sequence": ..., "delta": ...}
    {"event": "meeting_ask_user", "meeting_id": ..., "question": ...}
    {"event": "meeting_summary_draft", "meeting_id": ..., "summary": ...}

Robyn's WebSocketAdapter exposes no path params, so the route is
``/meetings/ws?meeting_id=...&session_id=...&token=...``. The token gate is the
shared ``WSPushChannel.serve`` handshake.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from pub.func.validator import is_safe_session_id
from robyn import WebSocketAdapter

from runtime import hooks
from server.trigger.core import app
from server.trigger.ws.push_channel import WSPushChannel

__all__ = [
    "MEETING_EVENTS",
    "meeting_ws_handler",
    "push_meeting_event",
]

#: The event names the frontend store handles.
MEETING_EVENTS = frozenset(
    {
        "meeting_speaker_changed",
        "meeting_message_delta",
        "meeting_message_completed",
        "meeting_thinking_delta",
        "meeting_ask_user",
        "meeting_summary_draft",
    }
)

_channel = WSPushChannel()


def push_meeting_event(meeting_id: str, event: dict[str, Any]) -> None:
    """Hook implementation: enqueue one event to the meeting's subscribers."""
    frame = json.dumps(
        {"meeting_id": meeting_id, **event},
        ensure_ascii=False,
        default=str,
    )
    _channel.enqueue_frame(frame, key=meeting_id)


def _ensure_hook_registered() -> None:
    """Register the push hook (last-writer-wins, idempotent re-assembly)."""
    hooks.register(hooks.MEETING_EVENT_PUSH, push_meeting_event)


_ensure_hook_registered()


async def _snapshot(meeting_id: str) -> dict[str, Any]:
    """Ready frame: the meeting's persisted message history."""
    messages: list[dict[str, Any]] = []
    try:
        from agent.meeting.persistence import get_meeting_store

        store = await get_meeting_store()
        messages = await store.list_messages(meeting_id)
    except Exception as exc:  # cold/absent store → empty snapshot
        logger.debug("meeting WS snapshot failed for {}: {}", meeting_id, exc)
    return {"meeting_id": meeting_id, "messages": messages}


@app.websocket("/meetings/ws")
async def meeting_ws_handler(websocket: WebSocketAdapter):
    query_params = getattr(websocket, "query_params", {}) or {}
    meeting_id = str(query_params.get("meeting_id", "") or "")
    session_id = str(query_params.get("session_id", "") or "")
    if not meeting_id or (session_id and not is_safe_session_id(session_id)):
        logger.warning("Meeting WS connection rejected: id={} session={}", meeting_id, session_id)
        await websocket.close()
        return
    await _channel.serve(
        websocket,
        ensure_registered=_ensure_hook_registered,
        handler_label="Meeting WS",
        client_label="Meeting WS",
        channel_key=meeting_id,
        ready_factory=lambda: _snapshot(meeting_id),
    )
