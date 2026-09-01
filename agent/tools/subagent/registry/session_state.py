"""Read-only busy/idle detection for a parent (main) agent session.

Answers a single question: is the session currently processing a turn? Four
independent signals, all keyed by the BARE session id, checked in the fixed
precedence order (frozen by tests/unit/state/test_detect_state_extension.py):

    ws_task > answering > hitl_pending > auto_turn_inflight > idle


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

3. HITL-wait flag (plan Task 2) — per-session registry OWNED by this module,
   mutated only via ``set_hitl_pending(session_id, value)`` (Task 7 wires the
   set/clear calls in the WS layer: set when the ``hitl_request`` frame is
   sent at ``server/trigger/ws/messages.py:67-74``, cleared when the resume
   turn starts at :181). Needed because during a HITL wait neither signal
   above is live — the WS task was popped (:114-118) and ``answering`` was
   cleared in the stream ``finally`` (:548) while the graph is still
   suspended awaiting ``hitl_response`` — so the session would misreport as
   idle.

4. Auto-turn in-flight — membership of ``server/service/auto_turn.py:51``
   ``_INFLIGHT: dict[str, asyncio.Task]`` (bare id -> auto-turn runner task).
   "Live" means present AND ``task.done()`` is False (same shape as the
   ws_task signal). Narrows the TOCTOU window (plan Task 2): the answering
   flag is only set at ``messages.py:273`` AFTER the heavy
   ``built_agent(force_rebuild=True)`` rebuild (:220), while ``_INFLIGHT``
   is registered at auto-turn dispatch time.

Session keys arrive from the announce side as ``agent:main:session:{id}``
while the structures above use bare ids, so every lookup goes through
``agent.tools.subagent.registry.session_keys.normalize_session_key`` (Task 1
frozen contract) first.

Guarantees:
    - READ-ONLY for signals 1/2/4: neither the WS task table, the state
      register, nor ``_INFLIGHT`` is ever mutated here;
    - the ONLY owned state is the signal-3 hitl registry, mutated through
      ``set_hitl_pending`` alone;
    - call-time truth only: no caching, no TTL — each call re-reads the
      sources (cheap dict/state lookups);
    - no heavy imports at module load: ``server.trigger.ws.messages`` (robyn +
      channel boot), ``runtime.state_register`` and
      ``server.service.auto_turn`` (pulls ``server.service.messages``) are
      imported lazily inside the accessors so this module stays importable
      in isolated contexts.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from agent.tools.subagent.registry.session_keys import normalize_session_key

__all__ = ["SessionState", "detect_state", "is_session_busy", "set_hitl_pending"]

REASON_WS_TASK = "ws_task"
REASON_ANSWERING = "answering"
REASON_HITL_PENDING = "hitl_pending"
REASON_AUTO_TURN_INFLIGHT = "auto_turn_inflight"
REASON_IDLE = "idle"


@dataclass(frozen=True)
class SessionState:
    """Result of one detection pass for a parent session."""

    session_id: str  # normalized bare id actually used for the lookups
    busy: bool
    reason: str  # "ws_task" | "answering" | "hitl_pending" | "auto_turn_inflight" | "idle"


_HITL_PENDING: set[str] = set()  # bare ids currently waiting on a HITL decision
_HITL_LOCK = threading.Lock()  # guards _HITL_PENDING mutations (cheap, never awaited)


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


def _get_auto_turn_module():
    """Lazy accessor for the auto-turn module (read-only use of _INFLIGHT).

    Lazy because ``server.service.auto_turn`` pulls in
    ``server.service.messages`` — far too heavy for module import time. Kept
    as a seam so tests can fake the registry without the real import chain.
    """
    from server.service import auto_turn as auto_turn_module

    return auto_turn_module


def set_hitl_pending(session_id: str, value: bool) -> None:
    """Set/clear the HITL-wait busy flag for a session (plan Task 2).

    Bare ids are normalized so announce-side prefixed keys hit the same
    entry. Empty/unknown ids are ignored. Task 7 wires the calls: set when
    the ``hitl_request`` frame goes out, cleared when the resume turn starts.
    """
    bare = normalize_session_key(session_id)
    if not bare:
        return
    with _HITL_LOCK:
        if value:
            _HITL_PENDING.add(bare)
        else:
            _HITL_PENDING.discard(bare)


def _is_hitl_pending(session_id: str) -> bool:
    with _HITL_LOCK:
        return session_id in _HITL_PENDING


def _is_auto_turn_inflight(session_id: str) -> bool:
    # Direct membership read of the module-level dict ("import and check
    # membership directly" — Task 2 spec); lock mirrors auto_turn's own
    # mutation discipline. Done tasks are stale entries, not live turns.
    mod = _get_auto_turn_module()
    with mod._INFLIGHT_LOCK:
        task = mod._INFLIGHT.get(session_id)
    return task is not None and not task.done()


def detect_state(session_key: str) -> SessionState:
    """Return the busy/idle state of a parent session with the deciding reason.

    Signal precedence (frozen by tests/unit/state/test_detect_state_extension.py,
    guardrail G5 — the original ws_task > answering order is unchanged):

        ws_task > answering > hitl_pending > auto_turn_inflight > idle

    hitl_pending outranks auto_turn_inflight because a HITL wait is the more
    specific state when both could be present (an auto turn may still be
    registered while its graph is suspended awaiting a HITL decision).
    """
    session_id = normalize_session_key(session_key)
    if _has_live_ws_task(session_id):
        return SessionState(session_id=session_id, busy=True, reason=REASON_WS_TASK)
    if _is_answering(session_id):
        return SessionState(session_id=session_id, busy=True, reason=REASON_ANSWERING)
    if _is_hitl_pending(session_id):
        return SessionState(session_id=session_id, busy=True, reason=REASON_HITL_PENDING)
    if _is_auto_turn_inflight(session_id):
        return SessionState(
            session_id=session_id, busy=True, reason=REASON_AUTO_TURN_INFLIGHT
        )
    return SessionState(session_id=session_id, busy=False, reason=REASON_IDLE)


def is_session_busy(session_key: str) -> bool:
    """True iff the parent session is currently processing a turn.

    Accepts announce-form keys (``agent:main:session:{id}``) and bare ids;
    empty/unknown ids are simply idle.
    """
    return detect_state(session_key).busy
