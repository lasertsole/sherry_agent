"""SQLite persistence layer for TaskFlow state (task_flows table).

Database path: agent/tools/taskflow/data/taskflow_registry.db
Table schema: task_flows(flow_id TEXT PK, state_json TEXT NOT NULL,
wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL,
child_session_key TEXT, total_tokens INTEGER DEFAULT 0,
total_cost REAL DEFAULT 0.0, token_budget INTEGER DEFAULT 0,
deadline_ts REAL, session_id TEXT NOT NULL DEFAULT '',
resumed_from TEXT, superseded_by TEXT, updated_at REAL, terminal_reason TEXT)

Connection lifecycle comes from ``agent.tools.pub_base.sqlite_store``
(``BaseSQLiteRepository``, the shared skeleton also used by the todolist and
subagent registries): EVERY connection (aiosqlite and stdlib sqlite3 alike) is
configured with a 5s busy timeout as its first statement (or the equivalent
connect ``timeout``), so contended writes wait for the lock instead of failing
with ``sqlite3.OperationalError: database is locked``. The WAL check/switch and
the ``CREATE TABLE IF NOT EXISTS`` statements run ONCE per process, lazy,
loop-gated under an asyncio.Lock on the async path and thread-locked on the
sync path, never per operation.

Optimistic locking: ALL mutations go through
``UPDATE ... WHERE flow_id = ? AND expected_revision = ?``. When the update
matches zero rows the write conflicted (or the flow vanished); the caller
gets :class:`FlowConflictError` carrying the latest revision so it can
re-read (taskflow_summary) and retry, or :class:`FlowNotFoundError`. One
deliberate exception: :func:`mark_flow_superseded` writes the lineage
back-pointer without a revision check (see its docstring).
"""

# allow: SIZE_OK — single store module for the task_flows table: schema DDL,
# row mapping, and every query share one connection and init lifecycle and
# cannot be split without duplicating that plumbing.

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

import aiosqlite
from loguru import logger

from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository
from config.features import TASKFLOW_INFRA
from ..config import INITIAL_REVISION, TABLE_NAME, TERMINAL_STATUSES, StepStatus, TaskFlowStatus

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "taskflow_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout, handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = TASKFLOW_INFRA["busy_timeout_ms"]
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see ensure_db).
_INIT_WAIT_TIMEOUT_S = TASKFLOW_INFRA["init_wait_timeout_s"]

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL,
    session_id TEXT NOT NULL DEFAULT '',
    resumed_from TEXT,
    superseded_by TEXT,
    updated_at REAL,
    terminal_reason TEXT
);
"""

_SELECT_COLUMNS_SQL = (
    f"SELECT flow_id, state_json, wait_json, expected_revision, status, child_session_key, "
    f"total_tokens, total_cost, token_budget, deadline_ts, session_id, "
    f"resumed_from, superseded_by, updated_at, terminal_reason "
    f"FROM {TABLE_NAME}"
)

# Query-coverage indexes (2026-09 SQLite index audit). Both are created with
# ``IF NOT EXISTS`` on every schema init -- async and sync alike.
# ``idx_taskflow_deadline`` is partial: get_overdue_flows only ever scans
# non-NULL deadlines, so NULL rows stay out of the index entirely.
# ``idx_taskflow_session_status`` covers the session-scoped reads
# (get_active_flows_sync / get_all_flows_sync filter on session_id and status).
_INDEX_DDL: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_taskflow_status ON {TABLE_NAME}(status)",
    f"CREATE INDEX IF NOT EXISTS idx_taskflow_deadline ON {TABLE_NAME}(deadline_ts) "
    "WHERE deadline_ts IS NOT NULL",
    f"CREATE INDEX IF NOT EXISTS idx_taskflow_session_status ON {TABLE_NAME}(session_id, status)",
)


# Once-per-process async schema-init state. asyncio primitives are single-loop
# by design, so the lock is only ever touched by the owning loop (see
# ensure_db); other loops poll _initialized instead of queueing on the lock.
_init_lock = asyncio.Lock()
_init_loop: asyncio.AbstractEventLoop | None = None
_initialized = False

# Once-per-process sync schema-init state (stdlib sqlite3 write paths may be
# called from arbitrary threads; threading.Lock is cross-thread safe).
_sync_tables_ready = False
_sync_init_lock = threading.Lock()


class _Unset:
    """Sentinel distinguishing 'leave the column unchanged' from None (clear)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<UNSET>"


UNSET = _Unset()


class TaskFlowStoreError(Exception):
    """Base class for taskflow store errors."""


class FlowExistsError(TaskFlowStoreError):
    def __init__(self, flow_id: str):
        self.flow_id = flow_id
        super().__init__(f"TaskFlow '{flow_id}' already exists")


class FlowNotFoundError(TaskFlowStoreError):
    def __init__(self, flow_id: str):
        self.flow_id = flow_id
        super().__init__(f"TaskFlow '{flow_id}' not found")


class FlowConflictError(TaskFlowStoreError):
    """Optimistic-lock conflict: the write matched zero rows.

    ``latest_revision`` is embedded in the message so callers (and the LLM)
    can re-read and retry without a second lookup.
    """

    def __init__(self, flow_id: str, expected_revision: int, latest_revision: int):
        self.flow_id = flow_id
        self.expected_revision = expected_revision
        self.latest_revision = latest_revision
        super().__init__(
            f"TaskFlow '{flow_id}' revision conflict: expected_revision={expected_revision} "
            f"but latest revision={latest_revision}; re-read with taskflow_inspect(action='summary') and "
            f"retry with expected_revision={latest_revision}"
        )


def _dump_json(value: dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load_json(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("taskflow store: unparseable JSON column value: {!r}", raw)
        return None


def _row_to_flow(row: tuple) -> dict:
    (
        flow_id,
        state_json,
        wait_json,
        expected_revision,
        status,
        child_session_key,
        total_tokens,
        total_cost,
        token_budget,
        deadline_ts,
        session_id,
        resumed_from,
        superseded_by,
        updated_at,
        terminal_reason,
    ) = row
    return {
        "flow_id": flow_id,
        "state": _load_json(state_json) or {},
        "wait": _load_json(wait_json),
        "expected_revision": int(expected_revision),
        "status": status,
        "child_session_key": child_session_key,
        "total_tokens": int(total_tokens or 0),
        "total_cost": float(total_cost or 0.0),
        "token_budget": int(token_budget or 0),
        "deadline_ts": float(deadline_ts) if deadline_ts is not None else None,
        "session_id": session_id or "",
        "resumed_from": resumed_from or None,
        "superseded_by": superseded_by or None,
        "updated_at": float(updated_at) if updated_at is not None else None,
        "terminal_reason": terminal_reason or None,
    }


class _TaskFlowRepository(BaseSQLiteRepository):
    """Base-class wiring for the task_flows store: DDL + indexes."""

    wal_label = "taskflow registry db"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_CREATE_TABLE_SQL,)

    def _index_ddls(self) -> tuple[str, ...]:
        return _INDEX_DDL


# The base reads/writes the module-level init state through this namespace, so
# the existing monkeypatch contract (``_DB_PATH``/``_initialized``/...) is intact.
_repository = _TaskFlowRepository(globals())

_connect = _repository.connect
_switch_to_wal_if_needed = _repository.switch_to_wal_if_needed
ensure_db = _repository.ensure_db
_ensure_tables_sync = _repository.ensure_tables_sync


async def create_flow(
    flow_id: str,
    state: dict,
    *,
    session_id: str,
    status: str = TaskFlowStatus.RUNNING.value,
    child_session_key: str | None = None,
    deadline_ts: float | None = None,
    resumed_from: str | None = None,
) -> dict:
    """Insert a new flow at INITIAL_REVISION; FlowExistsError on duplicate id.

    ``session_id`` is required: every row is owned by exactly one session and
    every session-scoped read filters on it. ``resumed_from`` records lineage
    (the predecessor flow this one continues); the matching ``superseded_by``
    back-pointer is written by :func:`mark_flow_superseded`.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        raise ValueError("flow_id must be a non-empty string")
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    await ensure_db()
    columns = [
        "flow_id",
        "state_json",
        "wait_json",
        "expected_revision",
        "status",
        "child_session_key",
        "session_id",
        "updated_at",
    ]
    placeholders = ["?", "?", "NULL", "?", "?", "?", "?", "?"]
    params: list[object] = [
        flow_id,
        _dump_json(state),
        INITIAL_REVISION,
        status,
        child_session_key,
        session_id,
        time.time(),
    ]
    if deadline_ts is not None:
        columns.append("deadline_ts")
        placeholders.append("?")
        params.append(float(deadline_ts))
    if resumed_from:
        columns.append("resumed_from")
        placeholders.append("?")
        params.append(resumed_from)
    try:
        async with _connect() as db:
            await db.execute(
                f"INSERT INTO {TABLE_NAME} ({', '.join(columns)}) "
                f"VALUES ({', '.join(placeholders)})",
                params,
            )
            await db.commit()
    except aiosqlite.IntegrityError as e:
        raise FlowExistsError(flow_id) from e
    flow = await get_flow(flow_id, session_id)
    if flow is None:  # unreachable: the row was inserted in this transaction
        raise FlowNotFoundError(flow_id)
    return flow


async def get_flow(flow_id: str, session_id: str) -> dict | None:
    """Read one session-owned flow with parsed JSON columns; None when absent."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE flow_id = ? AND session_id = ?",
            (flow_id, session_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_flow(row)


async def mark_flow_superseded(flow_id: str, superseded_by: str, *, session_id: str) -> bool:
    """Stamp a predecessor flow's ``superseded_by`` back-pointer.

    The back-pointer is lineage metadata and deliberately does NOT bump
    ``expected_revision`` (a caller holding the predecessor's revision is not
    invalidated). It DOES make an active predecessor terminal: a superseded flow
    must leave every active read, including the tri-color sweep's white set.
    Returns True when one row matched, False when the flow is absent FOR THIS
    SESSION.
    """
    terminal = tuple(sorted(TERMINAL_STATUSES))
    placeholders = ", ".join("?" for _ in terminal)
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET superseded_by = ?, updated_at = ?, "
            f"status = CASE WHEN status IN ({placeholders}) THEN status ELSE ? END, "
            f"terminal_reason = CASE WHEN status IN ({placeholders}) "
            f"THEN terminal_reason ELSE ? END "
            "WHERE flow_id = ? AND session_id = ?",
            (
                superseded_by,
                time.time(),
                *terminal,
                TaskFlowStatus.CANCELLED.value,
                *terminal,
                "superseded",
                flow_id,
                session_id,
            ),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_flow(
    flow_id: str,
    expected_revision: int,
    *,
    session_id: str,
    state: dict | None = None,
    wait: dict | None | _Unset = UNSET,
    status: str | None = None,
    child_session_key: str | None | _Unset = UNSET,
    total_tokens: int | None = None,
    total_cost: float | None = None,
    token_budget: int | None = None,
    deadline_ts: float | None | _Unset = UNSET,
    terminal_reason: str | None | _Unset = UNSET,
) -> dict:
    """Optimistic-locking mutation of one flow row owned by ``session_id``.

    Every call bumps ``expected_revision`` by exactly 1 and requires the row
    to still be at ``expected_revision``:

    * ``state``: replace state_json when not None.
    * ``wait``: replace wait_json; None clears it; UNSET keeps it.
    * ``status``: replace status when not None.
    * ``child_session_key``: replace when not UNSET.
    * ``total_tokens``/``total_cost``/``token_budget``: replace when not None.
    * ``deadline_ts``: replace when not UNSET and not None.
    * ``terminal_reason``: replace when not UNSET (None clears).

    Every successful call also stamps ``updated_at`` — the inactivity clock the
    liveness sweep reads.

    Returns the updated flow dict. Raises FlowNotFoundError when no flow with
    that id exists FOR THIS SESSION (a flow owned by another session is
    indistinguishable from a missing one), FlowConflictError (carrying the
    latest revision) when another writer won the race.
    """
    assignments: list[str] = []
    params: list[object] = []
    if state is not None:
        assignments.append("state_json = ?")
        params.append(_dump_json(state))
    if not isinstance(wait, _Unset):
        assignments.append("wait_json = ?")
        params.append(_dump_json(wait))
    if status is not None:
        assignments.append("status = ?")
        params.append(status)
    if not isinstance(child_session_key, _Unset):
        assignments.append("child_session_key = ?")
        params.append(child_session_key)
    if total_tokens is not None:
        assignments.append("total_tokens = ?")
        params.append(int(total_tokens))
    if total_cost is not None:
        assignments.append("total_cost = ?")
        params.append(float(total_cost))
    if token_budget is not None:
        assignments.append("token_budget = ?")
        params.append(int(token_budget))
    if not isinstance(deadline_ts, _Unset) and deadline_ts is not None:
        assignments.append("deadline_ts = ?")
        params.append(float(deadline_ts))
    if not isinstance(terminal_reason, _Unset):
        assignments.append("terminal_reason = ?")
        params.append(terminal_reason)
    if not assignments:
        raise ValueError("update_flow called with nothing to update")

    new_revision = int(expected_revision) + 1
    assignments.append("updated_at = ?")
    params.append(time.time())
    assignments.append("expected_revision = ?")
    params.append(new_revision)
    params.extend([flow_id, int(expected_revision), session_id])

    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET {', '.join(assignments)} "
            "WHERE flow_id = ? AND expected_revision = ? AND session_id = ?",
            params,
        )
        if cursor.rowcount == 0:
            await db.rollback()
            async with db.execute(
                f"SELECT expected_revision FROM {TABLE_NAME} WHERE flow_id = ? AND session_id = ?",
                (flow_id, session_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise FlowNotFoundError(flow_id)
            raise FlowConflictError(flow_id, int(expected_revision), int(row[0]))
        await db.commit()

    flow = await get_flow(flow_id, session_id)
    if flow is None:  # unreachable: the UPDATE matched this row
        raise FlowNotFoundError(flow_id)
    return flow


async def get_overdue_flows(now_ts: float) -> list[dict]:
    """Return non-terminal flows whose deadline_ts < now_ts."""
    await ensure_db()
    terminal = tuple(sorted(TERMINAL_STATUSES))
    placeholders = ", ".join("?" for _ in terminal)
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL
            + " WHERE deadline_ts IS NOT NULL AND deadline_ts < ? "
            + f"AND status NOT IN ({placeholders})",
            (now_ts, *terminal),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_flow(row) for row in rows]


async def get_non_terminal_flows(session_id: str | None = None) -> list[dict]:
    """Non-terminal, non-superseded flow rows (all sessions when ``session_id`` is None)."""
    await ensure_db()
    terminal = tuple(sorted(TERMINAL_STATUSES))
    placeholders = ", ".join("?" for _ in terminal)
    sql = _SELECT_COLUMNS_SQL + f" WHERE status NOT IN ({placeholders}) AND superseded_by IS NULL"
    params: tuple[object, ...] = terminal
    if session_id:
        sql += " AND session_id = ?"
        params = (*terminal, session_id)
    async with _connect() as db:
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_flow(row) for row in rows]


async def get_non_terminal_session_ids() -> list[str]:
    """Sessions that still own at least one non-terminal, non-superseded flow."""
    await ensure_db()
    terminal = tuple(sorted(TERMINAL_STATUSES))
    placeholders = ", ".join("?" for _ in terminal)
    async with _connect() as db:
        async with db.execute(
            f"SELECT DISTINCT session_id FROM {TABLE_NAME} "
            f"WHERE status NOT IN ({placeholders}) AND superseded_by IS NULL",
            terminal,
        ) as cursor:
            rows = await cursor.fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


async def _finalize_gc_flow(flow: dict, reason: str) -> bool:
    """Fail one collected flow and settle its unresolved steps.

    Used by the tri-color and liveness sweeps. A revision conflict means a
    concurrent tool call advanced the flow after the scan — that is progress,
    so this write is abandoned and the next tick re-evaluates the flow from
    fresh state instead of clobbering it with the stale snapshot.
    """
    flow_id = str(flow.get("flow_id") or "")
    session_id = str(flow.get("session_id") or "")
    if not flow_id or not session_id:
        return False

    state = dict(flow.get("state") or {})
    state["failure_reason"] = reason
    steps = list(state.get("steps") or [])
    terminal_steps = {
        StepStatus.DONE.value,
        StepStatus.FAILED.value,
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
    }
    detached: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("status") or "") in terminal_steps:
            continue
        key = str(step.get("child_session_key") or "").strip()
        if key and key not in detached:
            detached.append(key)
        step["status"] = StepStatus.FAILED.value
        step["fail_reason"] = reason
    state["steps"] = steps
    if detached:
        state["detached_child_session_keys"] = detached

    revision = int(flow.get("expected_revision") or 0)
    try:
        await update_flow(
            flow_id,
            revision,
            session_id=session_id,
            state=state,
            wait=None,
            status=TaskFlowStatus.FAILED.value,
            terminal_reason=reason,
        )
    except FlowConflictError as exc:
        logger.debug("TaskFlow '{}' GC skipped (concurrent progress): {}", flow_id, exc)
        return False
    except FlowNotFoundError:
        return False
    logger.warning("TaskFlow '{}' collected ({}); marked failed", flow_id, reason)
    return True


async def fail_flow_for_gc(flow: dict, *, reason: str) -> bool:
    """Fail one flow and settle its unresolved steps (tri-color/liveness/entry)."""
    return await _finalize_gc_flow(flow, reason)


async def fail_stale_flow(flow: dict, *, reason: str = "stale_swept") -> bool:
    """Liveness sweep: fail one flow already proven stale by the caller."""
    return await _finalize_gc_flow(flow, reason)


async def get_zombie_flow_ids(session_id: str) -> list[str]:
    """Flow ids that are failed OR cancelled (a pending todo on them is a zombie).

    Done flows are deliberately excluded: their todo may still be a legitimate
    review item, and marking it completed is the user's call.
    """
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT flow_id FROM {TABLE_NAME} WHERE session_id = ? AND status IN (?, ?)",
            (
                session_id,
                TaskFlowStatus.FAILED.value,
                TaskFlowStatus.CANCELLED.value,
            ),
        ) as cursor:
            rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def get_failed_flow_session_ids() -> list[str]:
    """Sessions owning a failed/cancelled flow (the zombie-todo sweep scope)."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT DISTINCT session_id FROM {TABLE_NAME} WHERE status IN (?, ?)",
            (TaskFlowStatus.FAILED.value, TaskFlowStatus.CANCELLED.value),
        ) as cursor:
            rows = await cursor.fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


async def count_total_flows_for_session(session_id: str) -> int:
    """Number of flow rows owned by a session (all statuses)."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def prune_session_flows(session_id: str) -> int:
    """Delete the oldest terminal/superseded rows past the per-session cap.

    Active rows are never deleted. ``max_total_flows_per_session <= 0`` disables
    pruning. Returns the number of deleted rows.
    """
    keep = int(TASKFLOW_INFRA["max_total_flows_per_session"])
    if keep <= 0:
        return 0
    total = await count_total_flows_for_session(session_id)
    excess = total - keep
    if excess <= 0:
        return 0
    terminal = tuple(sorted(TERMINAL_STATUSES))
    placeholders = ", ".join("?" for _ in terminal)
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM {TABLE_NAME} WHERE rowid IN ("
            f"  SELECT rowid FROM {TABLE_NAME} WHERE session_id = ? "
            f"  AND (status IN ({placeholders}) OR superseded_by IS NOT NULL) "
            f"  ORDER BY COALESCE(updated_at, 0) ASC LIMIT ?"
            f")",
            (session_id, *terminal, excess),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


async def get_flow_for_child_session(child_session_key: str) -> dict | None:
    """The non-terminal flow whose steps include this child session, if any.

    A flow dispatches one child per step, so the owning flow is found by
    scanning active flows' step records for the child key — this is what lets
    the announce pipeline resume the right flow when a dispatched child
    completes (a child has no direct flow pointer of its own).
    """
    key = (child_session_key or "").strip()
    if not key:
        return None
    await ensure_db()
    terminal = tuple(sorted(TERMINAL_STATUSES))
    placeholders = ", ".join("?" for _ in terminal)
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + f" WHERE status NOT IN ({placeholders})",
            terminal,
        ) as cursor:
            rows = await cursor.fetchall()
    for row in rows:
        flow = _row_to_flow(row)
        steps = list((flow.get("state") or {}).get("steps") or [])
        if any(
            isinstance(step, dict) and str(step.get("child_session_key") or "") == key
            for step in steps
        ):
            return flow
    return None


async def get_waiting_flows() -> list[dict]:
    """Return all flows in WAITING status."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE status = ?",
            (TaskFlowStatus.WAITING.value,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_flow(row) for row in rows]


def get_flow_sync(flow_id: str, session_id: str) -> dict | None:
    """Synchronously read one session-owned flow (stdlib sqlite3, no event loop).

    Mirrors the subagent blueprint's sync paths: threading.Lock-guarded
    one-time table creation, connect-level busy timeout, failures logged and
    swallowed with a None return.
    """
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            row = conn.execute(
                _SELECT_COLUMNS_SQL + " WHERE flow_id = ? AND session_id = ?",
                (flow_id, session_id),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return _row_to_flow(row)
    except Exception as e:
        logger.warning("Failed to sync-read taskflow {}: {}", flow_id, e)
    return None


def get_active_flows_sync(session_id: str) -> list[dict]:
    """Synchronously read one session's non-terminal flows (stdlib sqlite3).

    Returns flows with status 'running' or 'waiting' owned by ``session_id``.
    A superseded flow (``superseded_by`` set) is excluded even while its status
    is still non-terminal: the caller declared it replaced. The SQL filter is
    the isolation boundary; callers no longer post-filter by
    ``state['creator_session_key']``. Mirrors the sync read pattern of
    get_flow_sync(): threading.Lock-guarded table creation, connect-level busy
    timeout, failures logged and swallowed with an empty list return.
    """
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            cursor = conn.execute(
                _SELECT_COLUMNS_SQL
                + " WHERE session_id = ? AND status IN (?, ?)"
                + " AND superseded_by IS NULL"
                + " ORDER BY expected_revision DESC",
                (session_id, TaskFlowStatus.RUNNING.value, TaskFlowStatus.WAITING.value),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [_row_to_flow(row) for row in rows]
    except Exception as e:
        logger.warning("Failed to sync-read active taskflows: {}", e)
        return []


def get_all_flows_sync(session_id: str, status_filter: str = "active") -> list[dict]:
    """Synchronously read one session's flows for the session board (stdlib sqlite3).

    Scoped to ``session_id`` — the global cross-session view no longer exists;
    the sweeper's system-level scans use :func:`get_overdue_flows` /
    :func:`get_waiting_flows` instead.

    ``status_filter`` selects the rows:

    * ``"active"`` (default): running + waiting (delegates to
      :func:`get_active_flows_sync`).
    * ``"all"``: every flow of the session, terminal statuses included.
    * any other value: exact status match (``status = ?``), e.g. ``"done"``.

    Rows are ordered by ``expected_revision DESC`` (most recently active first).
    Fail-open: init/read failures are logged and swallowed, returning ``[]``.
    """
    if status_filter == "active":
        return get_active_flows_sync(session_id)
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            if status_filter == "all":
                cursor = conn.execute(
                    _SELECT_COLUMNS_SQL
                    + " WHERE session_id = ?"
                    + " ORDER BY expected_revision DESC",
                    (session_id,),
                )
            else:
                cursor = conn.execute(
                    _SELECT_COLUMNS_SQL
                    + " WHERE session_id = ? AND status = ?"
                    + " ORDER BY expected_revision DESC",
                    (session_id, status_filter),
                )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [_row_to_flow(row) for row in rows]
    except Exception as e:
        logger.warning("Failed to sync-read taskflows (filter={!r}): {}", status_filter, e)
        return []


async def delete_flows_by_session(session_id: str) -> int:
    """Delete every flow owned by ``session_id``; returns the row count.

    Session purge path (``server.DAO.messages.clear_session``): a cleared
    session leaves no TaskFlow debris behind. Rows with ``session_id = ''``
    (pre-isolation legacy rows) are never matched.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be a non-empty string")
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(f"DELETE FROM {TABLE_NAME} WHERE session_id = ?", (session_id,))
        await db.commit()
        return int(cursor.rowcount or 0)
