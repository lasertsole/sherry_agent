"""Per-turn trajectory events: the structured timeline an observability pane reads.

A turn emits a handful of discrete events (``turn_start`` / ``tool_start`` /
``tool_end`` / ``compact`` / ``error`` / ``turn_end``) — 3-12 per turn, never the
streaming deltas. ``tool_start`` and ``tool_end`` share one deterministic
``event_id`` so they UPPSERT into a single row carrying args AND result plus the
measured duration.

The table is deliberately NOT the messages table: messages store the
conversation (what the model reads), trajectory stores a queryable event
timeline (what a pane renders). Rows live next to the message store's SQLite file
and are pruned per session beyond ``max_turns_per_session``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from config.features import TRAJECTORY
from config.path import SESSIONS_DIR

__all__ = [
    "TrajectoryStore",
    "current_turn_id",
    "get_trajectory_store",
    "install_trajectory_hooks",
    "install_turn_id_hook",
    "project_frame",
    "record_event",
    "turn_scope",
    "turn_start_event",
    "turn_end_event",
]

#: The turn currently executing in THIS task (contextvar, so a resumed or
#: spawned turn gets its own value without threading a parameter through the
#: generator signatures every caller and test fake would have to accept).
current_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trajectory_turn_id", default=""
)


@contextlib.contextmanager
def turn_scope(turn_id: str, session_id: str) -> Iterator[None]:
    """Bind one turn's identity for BOTH the log scope and the ledger.

    ``logger.contextualize`` puts turn_id/session_id on every record produced
    inside the scope; the contextvar is what the frame projector reads at the
    stream's yield sites. One scope keeps the two in lockstep — a log line and a
    ledger row for the same event always carry the same turn.
    """
    token = current_turn_id.set(turn_id)
    try:
        with logger.contextualize(turn_id=turn_id, session_id=session_id):
            yield
    finally:
        current_turn_id.reset(token)


_DDL = """
CREATE TABLE IF NOT EXISTS trajectory_events (
    event_id     TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    turn_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    is_error     INTEGER DEFAULT 0,
    summary      TEXT,
    payload_json TEXT
)
"""

_DDL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_trajectory_session_turn "
    "ON trajectory_events(session_id, turn_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_trajectory_turn ON trajectory_events(turn_id, seq)",
)

#: Frames that become an event, and the kind each maps to.
_FRAME_KINDS = {
    "tool_start": "tool_start",
    "tool_result": "tool_end",
}
_IGNORED_FRAME_TYPES = frozenset({"text", "reasoning", "meta", "tool_end"})


def _clip(text: Any, limit: int) -> str:
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return value if len(value) <= limit else value[: limit - 1] + "…"


#: Per-session prune cadence: the retention policy is a slow background concern,
#: so a session pays for at most one prune per interval rather than per event.
_PRUNE_INTERVAL_S = 60.0

#: A reader (the trajectory tab) shares this file with the writer: wait instead
#: of failing. Matches the checkpointer's choice, at the tab's shorter horizon.
_BUSY_TIMEOUT_MS = 5_000


def _event_id(session_id: str, turn_id: str, kind: str, frame: dict[str, Any]) -> str:
    """Deterministic upsert key: one row per tool call, one per other event kind."""
    if kind in ("tool_start", "tool_end"):
        call_id = frame.get("tool_id") or frame.get("content") or "unknown"
        return f"{session_id}:{turn_id}:tool:{call_id}"
    if kind == "model_retry":
        # Retry progress is a SEQUENCE of transitions (scheduled → succeeded /
        # exhausted), not one event that gets refined: the attempt + status key
        # keeps each transition readable instead of the last one overwriting it.
        return f"{session_id}:{turn_id}:model_retry:{frame.get('attempt')}:{frame.get('status')}"
    return f"{session_id}:{turn_id}:{kind}"


def _payload(kind: str, frame: dict[str, Any]) -> tuple[str, bool, dict[str, Any]]:
    """``(summary, is_error, payload)`` for one frame."""
    if kind == "tool_start":
        return (
            str(frame.get("content") or frame.get("tool_name") or "tool"),
            False,
            {"tool_name": frame.get("content"), "args": frame.get("args", {})},
        )
    if kind == "tool_end":
        is_error = bool(frame.get("error"))
        return (
            str(frame.get("tool_name") or "tool"),
            is_error,
            {
                "result": frame.get("content", ""),
                "duration_ms": frame.get("duration_ms"),
                "error": is_error,
            },
        )
    return (kind, False, dict(frame))


class TrajectoryStore:
    """SQLite-backed event timeline (one shared DB file, per-session pruning)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = (
            Path(db_path) if db_path is not None else Path(SESSIONS_DIR) / "trajectory.db"
        )
        self._lock = asyncio.Lock()
        self._ready = False
        #: ``session_id -> monotonic`` of the last prune; see ``_prune_if_due``.
        self._last_prune: dict[str, float] = {}

    # -- schema -----------------------------------------------------------

    def _ddls(self) -> tuple[str, ...]:
        return (_DDL, *_DDL_INDEXES)

    async def _ensure(self) -> None:
        if self._ready:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        import aiosqlite

        async with aiosqlite.connect(self._db_path) as db:
            # WAL lives in the database header (set once), busy_timeout does NOT
            # (per connection, defaults to 0): the trajectory tab reads this file
            # while the agent writes it, so a reader would otherwise fail with
            # SQLITE_BUSY instead of waiting.
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            for ddl in self._ddls():
                await db.execute(ddl)
            await db.commit()
        self._ready = True

    # -- writes -----------------------------------------------------------

    async def append(
        self,
        session_id: str,
        turn_id: str,
        kind: str,
        frame: dict[str, Any],
        *,
        seq: int,
    ) -> None:
        """Upsert one event (fail-open — a metrics write never breaks a turn)."""
        if not TRAJECTORY["enabled"]:
            return
        summary, is_error, payload = _payload(kind, frame)
        payload_json = _clip(
            json.dumps(payload, ensure_ascii=False, default=str),
            int(TRAJECTORY["payload_max_chars"]),
        )
        event_id = _event_id(session_id, turn_id, kind, frame)
        try:
            await self._ensure()
            import aiosqlite

            async with self._lock, aiosqlite.connect(self._db_path) as db:
                # The upsert MERGES tool_end into its tool_start row: a tool call
                # is one row, not two — args from the start, result/duration from
                # the end.
                await db.execute(
                    """
                    INSERT INTO trajectory_events
                        (event_id, session_id, turn_id, seq, ts, kind, is_error, summary, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        is_error = excluded.is_error,
                        summary = excluded.summary,
                        payload_json = json_patch(
                            COALESCE(trajectory_events.payload_json, '{}'),
                            COALESCE(excluded.payload_json, '{}')
                        )
                    """,
                    (
                        event_id,
                        session_id,
                        turn_id,
                        seq,
                        datetime.now().isoformat(timespec="milliseconds"),
                        kind,
                        1 if is_error else 0,
                        _clip(summary, int(TRAJECTORY["result_clip_chars"])),
                        payload_json,
                    ),
                )
                await db.commit()
        except Exception:
            logger.debug("trajectory projection failed (fail-open)", exc_info=True)
            return
        await self._prune_if_due(session_id)

    async def _prune_if_due(self, session_id: str) -> None:
        """Apply the retention policy at most once per interval per session.

        ``prune_session`` used to be dead code while the docs promised
        "pruned on write" — the ledger grew without bound. Calling it per event
        would add a GROUP BY to every tool call, so it is debounced.
        """
        now = time.monotonic()
        if now - self._last_prune.get(session_id, 0.0) < _PRUNE_INTERVAL_S:
            return
        self._last_prune[session_id] = now
        await self.prune_session(session_id)

    async def prune_session(self, session_id: str) -> int:
        """Keep only the newest ``max_turns_per_session`` turns for a session."""
        keep_turns = int(TRAJECTORY["max_turns_per_session"])
        if keep_turns <= 0:
            return 0
        try:
            await self._ensure()
            import aiosqlite

            async with self._lock, aiosqlite.connect(self._db_path) as db:
                # Order by ROWID, not ``MAX(seq)``: the seq column is 0 for
                # every lifecycle row and never exceeds 0 today, so ordering by
                # it kept an ARBITRARY set of turns — the retention policy must
                # follow insertion order, which is the turn order.
                cursor = await db.execute(
                    "SELECT turn_id, MAX(rowid) AS last_row FROM trajectory_events "
                    "WHERE session_id = ? GROUP BY turn_id ORDER BY last_row DESC",
                    (session_id,),
                )
                rows: list[Any] = list(await cursor.fetchall())
                stale = [row[0] for row in rows[keep_turns:]]
                if stale:
                    placeholders = ",".join("?" for _ in stale)
                    await db.execute(
                        f"DELETE FROM trajectory_events WHERE session_id = ? "
                        f"AND turn_id IN ({placeholders})",
                        (session_id, *stale),
                    )
                    await db.commit()
                return len(stale)
        except Exception:
            logger.debug("trajectory pruning failed (fail-open)", exc_info=True)
            return 0

    async def delete_session(self, session_id: str) -> int:
        """Delete every trajectory event of a purged session."""
        self._last_prune.pop(session_id, None)
        try:
            await self._ensure()
            import aiosqlite

            async with self._lock, aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM trajectory_events WHERE session_id = ?", (session_id,)
                )
                await db.commit()
                return int(cursor.rowcount)
        except Exception:
            logger.debug("trajectory purge failed (fail-open)", exc_info=True)
            return 0

    # -- reads ------------------------------------------------------------

    async def read(
        self, session_id: str, *, turn_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Events ordered by seq (optionally one turn)."""
        try:
            await self._ensure()
            import aiosqlite

            async with aiosqlite.connect(self._db_path) as db:
                if turn_id:
                    cursor = await db.execute(
                        "SELECT event_id, turn_id, seq, ts, kind, is_error, summary, payload_json "
                        "FROM trajectory_events WHERE session_id = ? AND turn_id = ? "
                        "ORDER BY seq LIMIT ? OFFSET ?",
                        (session_id, turn_id, limit, offset),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT event_id, turn_id, seq, ts, kind, is_error, summary, payload_json "
                        "FROM trajectory_events WHERE session_id = ? "
                        "ORDER BY seq LIMIT ? OFFSET ?",
                        (session_id, limit, offset),
                    )
                rows = await cursor.fetchall()
        except Exception:
            logger.debug("trajectory read failed (fail-open)", exc_info=True)
            return []
        events: list[dict[str, Any]] = []
        for event_id, tid, seq, ts, kind, is_error, summary, payload_json in rows:
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except (TypeError, ValueError):
                payload = {}
            events.append(
                {
                    "event_id": event_id,
                    "turn_id": tid,
                    "seq": seq,
                    "ts": ts,
                    "kind": kind,
                    "is_error": bool(is_error),
                    "summary": summary,
                    "payload": payload,
                }
            )
        return events


_store: TrajectoryStore | None = None


def get_trajectory_store() -> TrajectoryStore:
    """The process-wide store (one DB file, one write lock)."""
    global _store
    if _store is None:
        _store = TrajectoryStore()
    return _store


def reset_trajectory_store() -> None:
    """Drop the singleton (tests / teardown)."""
    global _store
    _store = None


# One sequence per TURN, shared by both producers: the stream's frame projector
# and the agent-side hook events. It used to be two counters — the projector's
# per-turn int and a hardcoded 0 for hooks — so every hook event sorted BEFORE
# the turn's first frame and ``read()`` (ORDER BY seq) returned a scrambled
# timeline. Guarded by a lock because the sweep removes entries from other
# threads.
_TURN_SEQ: dict[str, int] = {}
_TURN_SEQ_LOCK = threading.Lock()


def next_turn_seq(turn_id: str) -> int:
    """The next sequence number for *turn_id* (1-based, process-local)."""
    with _TURN_SEQ_LOCK:
        value = _TURN_SEQ.get(turn_id, 0) + 1
        _TURN_SEQ[turn_id] = value
        return value


def _drop_turn_seq(turn_id: str) -> None:
    """Forget a finished turn's counter (the ledger keeps the numbers)."""
    with _TURN_SEQ_LOCK:
        _TURN_SEQ.pop(turn_id, None)


def turn_start_event(session_id: str, *, source: str, message_ids: list[str]) -> None:
    """Record a ``turn_start`` event for the turn the current scope names."""
    record_event(session_id, "turn_start", {"source": source, "message_ids": message_ids})


def turn_end_event(session_id: str, *, total_ms: float, ttfb_ms: float | None) -> None:
    """Record a ``turn_end`` event (total wall clock + time to first chunk)."""
    record_event(session_id, "turn_end", {"total_ms": round(total_ms), "ttfb_ms": ttfb_ms})
    turn_id = current_turn_id.get()
    if turn_id:
        _drop_turn_seq(turn_id)


def record_event(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Append one discrete event keyed by the CURRENT turn (contextvar), fail-open.

    This is the hook-side entry point (``runtime.hooks.RECORD_TRAJECTORY_EVENT``):
    agent-side producers call it with just what happened, and the turn identity —
    and the sequence number — come from the scope the turn driver opened.
    """
    turn_id = current_turn_id.get()
    if not turn_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        get_trajectory_store().append(
            session_id,
            turn_id,
            kind,
            {"type": kind, **payload},
            seq=next_turn_seq(turn_id),
        )
    )


def install_trajectory_hooks() -> None:
    """Register the ledger's recorder for agent-side producers (idempotent)."""
    from runtime import hooks

    hooks.register(hooks.RECORD_TRAJECTORY_EVENT, record_event)


def install_turn_id_hook() -> None:
    """Expose the active conversation-turn uuid to the message store.

    ``context_engine`` cannot import ``server``; it resolves this hook while
    persisting a batch and stamps ``messages.turn_id``. The contextvar is read
    at call time (inside ``asyncio.to_thread`` it is inherited from the turn's
    context), so a persisted row always carries the turn that produced it.
    Idempotent; an unassembled process resolves no hook and stamps ``""``.
    """
    from runtime import hooks

    def _current_turn_id() -> str:
        return current_turn_id.get()

    hooks.register(hooks.GET_CURRENT_TURN_ID, _current_turn_id)


def project_frame(
    frame: dict[str, Any], session_id: str, turn_id: str, seq: int
) -> asyncio.Task[None] | None:
    """Project one emitted WS frame onto the timeline (fail-open, off-loop write).

    Returns the write task when the frame carried an event (tests await it), else
    ``None``. The write is scheduled, never awaited, so a slow disk cannot stall
    the stream.
    """
    kind = _FRAME_KINDS.get(str(frame.get("type", "")))
    if kind is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None  # no loop (sync test context) — the timeline is best-effort
    return loop.create_task(
        get_trajectory_store().append(session_id, turn_id, kind, frame, seq=seq)
    )
