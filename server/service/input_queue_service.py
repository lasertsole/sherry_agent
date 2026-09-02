"""Atomic single-entry submission for parent-session turns (plan Task 5).

``submit_user_input`` is the ONE channel every inbound user/cron input must
flow through (G1): the WS layer (Task 7), cron/heartbeat origins (Task 8's
rewritten auto_turn), and channel adapters (Task 9) all call it instead of
hand-rolling their own detect-then-dispatch sequence.

Atomic flow (per-session asyncio.Lock removes the detect->dispatch TOCTOU):

    1. acquire the per-session lock (module-level dict, bounded cleanup)
    2. dedup pre-check: an ACTIVE queue row with the same ``client_msg_id``
       (any state) -> return DEDUPED, nothing else happens
    3. ``detect_state(session_id)`` (Task 2 signals: ws_task > answering >
       hitl_pending > auto_turn_inflight > idle) -- ANY busy=True reason
       queues; reasons are never special-cased here (G2: source and
       classification are fixed at the call site)
    4. busy branch: ``UserInputQueue.enqueue(...)`` -> QUEUED(position);
       ``QueueFullError`` -> QUEUE_FULL (the caller owns the error frame)
    5. idle branch: resolve the route's TurnExecutor FIRST (fail fast, no
       state mutated on a missing registration), then insert a CLAIMED
       placeholder row via ``UserInputQueue.insert_claimed`` -- the durable
       "turn in progress" fact, written in the SAME critical section as the
       idle check so a racing submit sees busy and enqueues (kills TOCTOU;
       the placeholder also covers the window between lock release and the
       executor setting its own busy flags)
    6. release the lock, THEN ``asyncio.create_task(executor.execute(...))``
       -- the whole turn never runs inside submit; Task 7's TurnRunner owns
       turn completion (on_turn_finished) and drains the QUEUED rows

The idle/busy decision treats a live CLAIMED row as busy even when
``detect_state`` reports idle (crash-recovery leftover placeholders count as
"turn in flight" until they expire or are voided), while leftover QUEUED rows
do NOT block a fresh turn -- stale QUEUED rows after a crash are drained
after the new turn finishes (Task 7), they never silence the session.

Crash recovery: the CLAIMED row is the audit/recovery fact. If the process
dies between step 5 and 6 the row survives in SQLite and ``recover()``
(24h expiry) eventually voids it; Task 10 wires startup reconciliation.

Public surface consumed by Tasks 7/9/10 (names are a contract):
``submit_user_input`` / ``SubmitResult`` / ``SubmitStatus`` / ``TurnExecutor``
/ ``TurnExecutorRegistry`` / ``OutboundRouter`` / ``get_default_registry`` /
``get_default_queue``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from agent.tools.subagent.registry.session_keys import normalize_session_key
from agent.tools.subagent.registry.session_state import SessionState, detect_state
from loguru import logger
from server.queue.user_input_queue import (
    QueueFullError,
    UserInputQueue,
    UserInputQueueStatus,
)

__all__ = [
    "DEDUPED",
    "QUEUE_FULL",
    "ROUTE_CHANNEL",
    "ROUTE_WS",
    "STARTED",
    "OutboundRouter",
    "SubmitResult",
    "SubmitStatus",
    "TurnExecutor",
    "TurnExecutorRegistry",
    "get_default_queue",
    "get_default_registry",
    "queued",
    "route_for",
    "submit_user_input",
]

Source = Literal["user", "cron"]

ROUTE_WS = "ws"  # reply_target is None (websocket session)
ROUTE_CHANNEL = "channel"  # reply_target carries channel routing JSON


class SubmitStatus(str, Enum):
    """Outcome of one ``submit_user_input`` call."""

    STARTED = "STARTED"
    QUEUED = "QUEUED"
    QUEUE_FULL = "QUEUE_FULL"
    DEDUPED = "DEDUPED"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Result of an atomic submit.

    ``position`` is the 1-based FIFO rank among the session's QUEUED rows and
    is meaningful only for ``QUEUED`` (0 otherwise).
    """

    status: SubmitStatus
    position: int = 0


STARTED = SubmitResult(SubmitStatus.STARTED)
QUEUE_FULL = SubmitResult(SubmitStatus.QUEUE_FULL)
DEDUPED = SubmitResult(SubmitStatus.DEDUPED)


def queued(position: int) -> SubmitResult:
    """Build the QUEUED(position) result ("you are #N in line")."""
    return SubmitResult(SubmitStatus.QUEUED, position)


@runtime_checkable
class TurnExecutor(Protocol):
    """Drives one full agent turn for a session.

    Task 7 provides the real implementation (generalizes auto_turn's
    ``_drive_turn`` pattern); tests inject fakes. Dispatch happens AFTER the
    per-session lock is released, via ``asyncio.create_task`` -- implementers
    own their own busy-flag lifecycle (answering/hitl_pending) and turn
    completion (Task 7's on_turn_finished drains the queue).
    """

    async def execute(
        self, session_id: str, message: str, source: Source, reply_target: str | None
    ) -> None: ...


@runtime_checkable
class OutboundRouter(Protocol):
    """Delivers one outbound frame to a session's client.

    Contract seam for Tasks 7/9: the WS layer and channel adapters register
    routers that forward ``queued`` / ``started`` / ``queue_full`` style
    notification frames. ``submit_user_input`` itself sends no frames --
    frame formats and error-frame conventions stay with the callers.
    """

    async def send(self, session_id: str, frame: Mapping[str, object]) -> None: ...


class TurnExecutorRegistry:
    """Route -> TurnExecutor registry (``"ws"`` / ``"channel"`` today).

    Task 7 registers the real WS turn runner via
    ``get_default_registry().register("ws", runner)``; Task 9 registers the
    channel runner. Tests inject fakes into their own registry instance and
    pass it explicitly to ``submit_user_input``.
    """

    def __init__(self) -> None:
        self._executors: dict[str, TurnExecutor] = {}

    def register(self, route: str, executor: TurnExecutor) -> None:
        """Bind ``executor`` to ``route`` (re-registration replaces)."""
        if not route:
            raise ValueError("route must be a non-empty string")
        self._executors[route] = executor

    def resolve(self, route: str) -> TurnExecutor | None:
        """The executor bound to ``route``, or None when unregistered."""
        return self._executors.get(route)


def route_for(reply_target: str | None) -> str:
    """Route key for a submission: NULL reply_target = WS path, else channel.

    Mirrors the queue schema (``reply_target`` is NULL on the WS path).
    """
    return ROUTE_CHANNEL if reply_target else ROUTE_WS


# Per-session critical sections. The dict is bounded: once it exceeds
# _LOCK_SWEEP_THRESHOLD entries, locks nobody currently holds are dropped on
# the next lookup. The sweep is synchronous on the owning event loop, so a
# caller between _get_session_lock() and its (uninterrupted) acquire keeps
# its own reference and is unaffected by the dict entry being dropped.
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_LOCK_SWEEP_THRESHOLD = 256


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """The per-session lock, creating it (and sweeping stale entries) lazily."""
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = _SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
    if len(_SESSION_LOCKS) > _LOCK_SWEEP_THRESHOLD:
        for key, held in list(_SESSION_LOCKS.items()):
            if key != session_id and not held.locked():
                _SESSION_LOCKS.pop(key, None)
    return lock


# Process-wide defaults. Task 7 wires the real ws TurnExecutor into
# get_default_registry() at startup; until then an idle submit fails fast
# with a clear RuntimeError instead of orphaning a placeholder row.
_default_queue: UserInputQueue | None = None
_default_registry = TurnExecutorRegistry()


def get_default_queue() -> UserInputQueue:
    """The process-wide store (same SQLite file as PendingInjectionStore)."""
    global _default_queue
    if _default_queue is None:
        _default_queue = UserInputQueue()
    return _default_queue


def get_default_registry() -> TurnExecutorRegistry:
    """The process-wide executor registry (Tasks 7/9 register real ones)."""
    return _default_registry


def _payload_json(message: str) -> str:
    """Serialized user-message JSON, isomorphic to the WS frame text payload."""
    return json.dumps({"text": message, "image_base64_list": []}, ensure_ascii=False)


async def _run_executor(
    executor: TurnExecutor, session_id: str, message: str, source: Source, reply_target: str | None
) -> None:
    """Fire-and-forget wrapper: a crashing executor must not die silently."""
    try:
        await executor.execute(session_id, message, source, reply_target)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - background task, log + keep serving
        logger.exception("turn executor crashed for session {}", session_id)


async def submit_user_input(
    session_id: str,
    message: str,
    source: Source,
    reply_target: str | None = None,
    client_msg_id: str | None = None,
    *,
    queue: UserInputQueue | None = None,
    executor_registry: TurnExecutorRegistry | None = None,
) -> SubmitResult:
    """Atomically start a turn or queue the input for the parent session.

    The single entry point for all inbound user/cron input (G1). Exactly one
    of: STARTED (turn dispatched), QUEUED(position), QUEUE_FULL, DEDUPED.
    Raises only for caller bugs: bad ``source``/empty ``session_id``
    (ValueError) or an unregistered TurnExecutor on the idle branch
    (RuntimeError, before any state mutation).

    ``queue`` / ``executor_registry`` keyword overrides exist for tests and
    future multi-store needs; production callers use the defaults.
    """
    if source not in ("user", "cron"):
        raise ValueError(f"source must be 'user' or 'cron', got {source!r}")
    bare = normalize_session_key(session_id)
    if not bare:
        raise ValueError(f"session_id must be a non-empty string, got {session_id!r}")

    store = queue if queue is not None else get_default_queue()
    registry = executor_registry if executor_registry is not None else get_default_registry()

    lock = _get_session_lock(bare)
    async with lock:
        # 1. Idempotency first: an ACTIVE row with this client_msg_id (any
        #    session) means the submission already lives in the queue or an
        #    in-flight turn -- no second row, no second dispatch.
        if (
            client_msg_id is not None
            and await store.find_active_by_client_msg_id(client_msg_id) is not None
        ):
            logger.debug("submit_user_input: dedup hit for client_msg_id {} ({})", client_msg_id, bare)
            return DEDUPED

        # 2. Busy decision, inside the lock: detect_state OR a live CLAIMED
        #    placeholder (a turn was dispatched but has not raised any
        #    detect_state signal yet -- the placeholder is the durable busy
        #    fact covering exactly that gap).
        state: SessionState = detect_state(session_id)
        placeholder_in_flight = any(
            row.status is UserInputQueueStatus.CLAIMED
            for row in await store.list_active(bare)
        )
        if state.busy or placeholder_in_flight:
            try:
                _row, position = await store.enqueue(
                    bare,
                    _payload_json(message),
                    source,
                    reply_target=reply_target,
                    client_msg_id=client_msg_id,
                )
            except QueueFullError:
                logger.warning("submit_user_input: queue full for session {}", bare)
                return QUEUE_FULL
            logger.info(
                "submit_user_input: session {} busy ({}); queued at position {}",
                bare,
                state.reason,
                position,
            )
            return queued(position)

        # 3. Idle branch: resolve the executor BEFORE mutating any state so a
        #    missing registration fails fast without orphaning a placeholder.
        route = route_for(reply_target)
        executor = registry.resolve(route)
        if executor is None:
            raise RuntimeError(
                f"no TurnExecutor registered for route {route!r}; "
                f"register one via get_default_registry().register({route!r}, executor)"
            )
        try:
            await store.insert_claimed(
                bare,
                _payload_json(message),
                source,
                reply_target=reply_target,
                client_msg_id=client_msg_id,
            )
        except QueueFullError:
            logger.warning("submit_user_input: queue full for session {} (placeholder)", bare)
            return QUEUE_FULL

    # 4. Lock released: dispatch OUTSIDE the critical section. The CLAIMED
    #    row written above is the crash-recovery fact; Task 7's TurnRunner
    #    owns completion (on_turn_finished) and queue draining.
    asyncio.create_task(
        _run_executor(executor, bare, message, source, reply_target),
        name=f"turn-executor:{bare}",
    )
    logger.info("submit_user_input: session {} idle -> turn dispatched (route={})", bare, route)
    return STARTED
