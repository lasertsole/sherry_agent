"""Per-session steering queue runtime for subagent-completion injections (plan task 6).

Runtime half of the durable injection queue built on task 3's
``PendingInjectionStore``: busy-session steering messages are queued in memory
AND persisted to SQLite in one call, and drained into a parent turn by the task
7 ``before_model`` middleware. Crash recovery is rehydration-on-first-access
from SQLite — there is deliberately NO background sweeper/daemon.

Concurrency model — copied from ``events/core.py`` (EventBus), the only proven
cross-loop queue precedent in this codebase:

- the backing store is ``collections.deque`` (thread-safe append/popleft), NOT
  asyncio.Queue/Event — queue users may sit on different event loops (WS turn
  loop, subagent task loop), and asyncio primitives would bind to one loop;
- plain ``threading.Lock`` guards only the multi-step memory sections
  (take-all, registry creation). It is NEVER held across an ``await``: SQLite
  calls (task 3 API, aiosqlite) happen strictly outside the lock;
- same-loop delivery is the confirmed production shape (Metis finding: the
  subagent task loop == the parent turn's server loop), so no cross-thread
  handoff machinery is needed.

Session keys arrive in announce form ``agent:main:session:{id}`` and are
normalized via task 1 ``normalize_session_key`` before use as dict keys (the
registry side uses bare ids); child/swarm keys carry no session prefix and pass
through unchanged, forming their own queues.

Element carrier type is the task 4 ``build_completion_message()`` product: a
``HumanMessage`` whose frozen metadata contract carries
``{internal, provenance, run_id, status}``. Each memory element pairs that
message with a ``PendingInjection`` mirror (the persistable form).

Known API gap (task 3 file is frozen — recorded, not fixed here):
    ``PendingInjection`` has no message-level completion-status field
    (completed/failed/interrupted). Messages rebuilt during rehydration
    therefore carry metadata ``{internal, provenance, run_id}`` WITHOUT
    ``status``; the original status remains visible inside the stored marker
    text ``"[subagent:{name} {status}]"``. Task 7's skip check only needs
    ``internal`` + ``provenance``, so this is informational loss only.
    Related: ``PendingInjectionStore.list_pending()`` has no session filter —
    rehydration filters rows client-side by ``requester_session_key``.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage, HumanMessage
from loguru import logger

from ..registry.pending_injections import PendingInjection, PendingInjectionStore
from ..registry.session_keys import normalize_session_key

__all__ = [
    "SteeringItem",
    "SteeringQueue",
    "drain",
    "enqueue_steering",
    "get_steering_queue",
    "rehydrate",
]

_PROVENANCE = "subagent_completion"


def _message_text(message: BaseMessage) -> str:
    """Return the message content as plain text (builder output is always str)."""
    content = message.content
    if isinstance(content, str):
        return content
    return message.text


@dataclass
class SteeringItem:
    """One steering/completion injection awaiting (or just taken from) a queue.

    ``message`` is the task 4 carrier injected into the parent turn by the task
    7 middleware; ``record`` is the task 3 persistable mirror (also the shape
    restored from SQLite on rehydration). ``consumed`` is set during ``drain``:
    ``True`` only when THIS drain call transitioned the SQLite row to CONSUMED.
    """

    record: PendingInjection
    message: HumanMessage
    consumed: bool = False

    @property
    def run_id(self) -> str:
        """Idempotency key of the underlying injection record."""
        return self.record.run_id


def _rebuild_message(record: PendingInjection) -> HumanMessage:
    """Rebuild the carrier message from a rehydrated record.

    The record's ``content`` is the full original message text (marker line
    included), so the rebuilt message is content-identical to the pre-crash
    original. See the module docstring for the missing-metadata-``status`` gap.
    """
    return HumanMessage(
        content=record.content,
        metadata={
            "internal": True,
            "provenance": _PROVENANCE,
            "run_id": record.run_id,
        },
    )


@dataclass
class _SessionState:
    """Per-session memory queue: deque + run_id index + a plain threading.Lock."""

    key: str
    items: deque[SteeringItem] = field(default_factory=deque)
    by_run_id: dict[str, SteeringItem] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    hydrated: bool = False
    hydrating: bool = False


class SteeringQueue:
    """Per-session in-memory steering queues with task 3 SQLite persistence.

    Open one instance per process (the module-level ``get_steering_queue()``
    singleton in production; per-test instances in the suite). A fresh instance
    pointed at the same db rehydrates PENDING rows on first access per session
    — that IS the crash-recovery contract.
    """

    def __init__(self, store: PendingInjectionStore | None = None) -> None:
        self._store: PendingInjectionStore = (
            store if store is not None else PendingInjectionStore()
        )
        self._states: dict[str, _SessionState] = {}
        self._states_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def store(self) -> PendingInjectionStore:
        """The task 3 store backing this queue (exposed for tests/verification)."""
        return self._store

    async def enqueue_steering(self, session_key: str, injection: BaseMessage) -> SteeringItem | None:
        """Queue one injection for ``session_key``: memory first, then SQLite.

        ``injection`` is the task 4 builder product; its frozen metadata must
        carry ``run_id`` (ValueError otherwise). Both steps happen here so the
        announce-flow caller stays zero-distraction.

        Idempotent on ``run_id``:

        - duplicate while a queued item exists → memory keeps the FIRST
          message; the SQLite write is a no-op (INSERT OR IGNORE); returns the
          existing item;
        - duplicate whose SQLite row is already CONSUMED → never re-queued
          (task 3 contract: consumed rows are not revived); the just-appended
          memory element is rolled back and ``None`` is returned.

        Returns the queued item for ``run_id``, or ``None`` when the run was
        already consumed.
        """
        bare = normalize_session_key(session_key)
        record = self._record_from_message(injection, bare)
        state = self._get_state(bare)
        await self._ensure_hydrated(state)

        with state.lock:
            existing = state.by_run_id.get(record.run_id)
            if existing is None:
                item = SteeringItem(record=record, message=self._as_carrier(injection))
                state.items.append(item)
                state.by_run_id[record.run_id] = item
            else:
                item = existing

        persisted = await self._store.enqueue(record)
        if persisted.status.value == "consumed":
            # Lost a race against a competing delivery path (row already
            # consumed): drop the just-appended element so memory matches the
            # durable truth — consumed runs are never re-injected.
            self._discard(state, record.run_id)
            return None

        if existing is not None:
            logger.debug(
                "steering queue: duplicate enqueue for run {} on session {} (memory kept first message)",
                record.run_id,
                bare,
            )
        else:
            logger.debug(
                "steering queue: enqueued run {} for session {} (memory + sqlite)",
                record.run_id,
                bare,
            )
        return item

    async def drain(self, session_key: str) -> list[SteeringItem]:
        """Atomically take ALL queued items and consume their SQLite rows.

        Take-all happens under the per-session lock (no awaits inside); each
        item is then transitioned to CONSUMED via the task 3 guarded UPDATE.
        The per-item ``consumed`` flag reports the true mark_consumed outcome
        (``False`` when the row was unknown/already consumed/consumption
        failed) — items are always returned, never silently dropped.

        Timing of WHEN this gets called is the task 7 middleware's decision;
        this method is a pure queue primitive.
        """
        bare = normalize_session_key(session_key)
        state = self._get_state(bare)
        await self._ensure_hydrated(state)

        with state.lock:
            items = list(state.items)
            state.items.clear()
            state.by_run_id.clear()

        if not items:
            return []

        for item in items:
            try:
                item.consumed = await self._store.mark_consumed(item.run_id)
            except Exception as e:  # noqa: BLE001 - per-item honesty beats failing the whole drain
                logger.warning(
                    "steering queue: mark_consumed failed for run {}: {}", item.run_id, e
                )
                item.consumed = False
        return items

    async def rehydrate(self, session_key: str) -> list[SteeringItem]:
        """Load PENDING rows for ``session_key`` from SQLite on first access.

        The crash-recovery entry point: a queue instance that has never touched
        this session pulls its PENDING records (filtered by
        ``requester_session_key``) into memory. Subsequent calls are no-ops —
        hydration happens exactly once per (instance, session). Returns a
        snapshot of the session's queued items after hydration.
        """
        bare = normalize_session_key(session_key)
        state = self._get_state(bare)
        await self._ensure_hydrated(state)
        with state.lock:
            return list(state.items)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _record_from_message(injection: BaseMessage, bare_key: str) -> PendingInjection:
        """Derive the persistable task 3 record from a task 4 carrier message."""
        meta = getattr(injection, "metadata", None) or {}
        run_id = meta.get("run_id")
        if not run_id:
            raise ValueError(
                "steering injection message must carry metadata['run_id'] (task 4 build_completion_message contract)"
            )
        return PendingInjection(
            run_id=str(run_id),
            requester_session_key=bare_key,
            content=_message_text(injection),
        )

    @staticmethod
    def _as_carrier(injection: BaseMessage) -> HumanMessage:
        """Coerce the injected message to the HumanMessage carrier type."""
        if isinstance(injection, HumanMessage):
            return injection
        return HumanMessage(
            content=_message_text(injection),
            metadata=getattr(injection, "metadata", None) or {},
        )

    def _get_state(self, bare_key: str) -> _SessionState:
        """Return (creating if absent) the per-session state for a bare key."""
        with self._states_lock:
            state = self._states.get(bare_key)
            if state is None:
                state = _SessionState(key=bare_key)
                self._states[bare_key] = state
            return state

    async def _ensure_hydrated(self, state: _SessionState) -> None:
        """Hydrate once per (instance, session) from SQLite PENDING rows.

        Same-loop guard via the ``hydrating`` flag (set synchronously before
        the first await): concurrent first-touch callers on one loop collapse
        to a single hydration pass. No asyncio primitives, no background task.
        """
        if state.hydrated or state.hydrating:
            return
        state.hydrating = True
        try:
            rows = await self._store.list_pending()
        except Exception as e:  # noqa: BLE001 - best-effort hydration; next access retries
            logger.warning("steering queue: rehydrate failed for session {}: {}", state.key, e)
            return
        finally:
            state.hydrating = False

        with state.lock:
            for row in rows:
                if normalize_session_key(row.requester_session_key) != state.key:
                    continue
                if row.run_id in state.by_run_id:
                    continue
                item = SteeringItem(record=row, message=_rebuild_message(row))
                state.items.append(item)
                state.by_run_id[row.run_id] = item
        state.hydrated = True

    @staticmethod
    def _discard(state: _SessionState, run_id: str) -> None:
        """Remove one element from the session queue (rollback after lost race)."""
        with state.lock:
            item = state.by_run_id.pop(run_id, None)
            if item is not None:
                try:
                    state.items.remove(item)
                except ValueError:  # already gone — nothing to roll back
                    pass


# ----------------------------------------------------------------------
# Module-level singleton (per-process, EventBus-style)
# ----------------------------------------------------------------------
# Holder-dict instead of ``global _QUEUE`` rebinding: basedpyright flags
# constant redefinition on globals; dict-key assignment keeps it clean.
_QUEUE_HOLDER: dict[str, SteeringQueue] = {}
_QUEUE_LOCK = threading.Lock()


def get_steering_queue() -> SteeringQueue:
    """Return the process-wide singleton ``SteeringQueue`` (default db path)."""
    queue = _QUEUE_HOLDER.get("queue")
    if queue is None:
        with _QUEUE_LOCK:
            queue = _QUEUE_HOLDER.get("queue")
            if queue is None:
                queue = SteeringQueue()
                _QUEUE_HOLDER["queue"] = queue
    return queue


async def enqueue_steering(session_key: str, injection: HumanMessage) -> SteeringItem | None:
    """Queue one injection for ``session_key`` on the process-wide singleton.

    Module-level convenience wrapper (frozen plan API): the task 7 middleware
    and the announce flow can call it without owning a ``SteeringQueue``.
    """
    return await get_steering_queue().enqueue_steering(session_key, injection)


async def drain(session_key: str) -> list[SteeringItem]:
    """Atomically take ALL items for ``session_key`` from the process-wide singleton."""
    return await get_steering_queue().drain(session_key)


async def rehydrate(session_key: str) -> list[SteeringItem]:
    """First-access hydration + queue snapshot for ``session_key`` (singleton)."""
    return await get_steering_queue().rehydrate(session_key)
