"""Read-only busy/idle detection for a parent (main) agent session.

Answers a single question: is the session currently processing a turn? Two
independent signals, both keyed by the BARE session id:

1. Live WS stream task — ``server/trigger/ws/messages.py:19``
   ``_active_tasks: dict[str, asyncio.Task]``. The ``/sessions/agent/ws``
   handler (:139) registers the background stream task under the
   client-supplied bare ``session_id`` for both turn paths (generate :228,
   HITL resume :181). Unregistration: the ``_run_stream`` ``finally`` pops the
   entry when the running task itself finishes (:114-118) and the socket
   disconnect reaper pops already-finished tasks (:242-246). "Live" means the
   key is present AND ``task.done()`` is False — the same shape
   ``_cancel_session`` (:121-136) treats as cancellable. A stale done entry
   falls through to the answering signal instead of reporting a false busy.

2. Answering flag — ``server/service/messages.py:241``
   ``state_register_mem.set_state(session_id, "answering", True)`` (bare id)
   covers both turn paths (generate and ``resume_agent`` :621); it is reset to
   False in the stream ``finally`` (:516/:802) and on WS stop (:135). Truthy
   = answering, matching the in-stream abort check
   ``get_state(...) is False`` (:258/:634).

Session keys arrive from the announce side as ``agent:main:session:{id}``
while both structures above use bare ids, so every lookup goes through
``agent.tools.subagent.registry.session_keys.normalize_session_key`` (Task 1
frozen contract) first.

Guarantees:
    - strictly READ-ONLY: neither structure is ever mutated;
    - call-time truth only: no caching, no TTL — each call re-reads both
      sources (cheap dict/state lookups);
    - no heavy imports at module load: ``server.trigger.ws.messages`` (robyn +
      channel boot) and ``runtime.state_register`` are imported lazily inside
      the accessors so this module stays importable in isolated contexts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent.tools.subagent.registry.session_keys import normalize_session_key

__all__ = ["SessionState", "detect_state", "is_session_busy"]

REASON_WS_TASK = "ws_task"
REASON_ANSWERING = "answering"
REASON_IDLE = "idle"


@dataclass(frozen=True)
class SessionState:
    """Result of one detection pass for a parent session."""

    session_id: str  # normalized bare id actually used for the lookups
    busy: bool
    reason: str  # "ws_task" | "answering" | "idle"


def _get_active_tasks() -> dict[str, asyncio.Task[Any]]:
    """Lazy accessor for the WS module's task table (read-only use)."""
    from server.trigger.ws import messages as ws_messages

    return ws_messages._active_tasks


def _get_state_register():
    """Lazy accessor for the in-memory state register (read-only use)."""
    from runtime.state_register import state_register_mem

    return state_register_mem


def _has_live_ws_task(session_id: str) -> bool:
    task = _get_active_tasks().get(session_id)
    return task is not None and not task.done()


def _is_answering(session_id: str) -> bool:
    # Truthiness matches the existing readers: the stream aborts on
    # ``get_state(...) is False``, so any truthy value means "answering".
    return bool(_get_state_register().get_state(session_id, "answering"))


def detect_state(session_key: str) -> SessionState:
    """Return the busy/idle state of a parent session with the deciding reason.

    Signal precedence: a live WS task wins over the answering flag (the task
    is the more specific evidence of an in-flight turn).
    """
    session_id = normalize_session_key(session_key)
    if _has_live_ws_task(session_id):
        return SessionState(session_id=session_id, busy=True, reason=REASON_WS_TASK)
    if _is_answering(session_id):
        return SessionState(session_id=session_id, busy=True, reason=REASON_ANSWERING)
    return SessionState(session_id=session_id, busy=False, reason=REASON_IDLE)


def is_session_busy(session_key: str) -> bool:
    """True iff the parent session is currently processing a turn.

    Accepts announce-form keys (``agent:main:session:{id}``) and bare ids;
    empty/unknown ids are simply idle.
    """
    return detect_state(session_key).busy
