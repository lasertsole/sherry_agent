"""SQLite persistence layer for TaskFlow state (task_flows table).

Database path: agent/tools/taskflow/data/taskflow_registry.db
Table schema: task_flows(flow_id TEXT PK, state_json TEXT NOT NULL,
wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL,
child_session_key TEXT)

Connection lifecycle mirrors the subagent registry blueprint
(agent/tools/subagent/registry/store_sqlite.py): EVERY connection (aiosqlite
and stdlib sqlite3 alike) is configured with a 5s busy timeout as its first
statement (or the equivalent connect ``timeout``), so contended writes wait
for the lock instead of failing with ``sqlite3.OperationalError: database is
locked``. The WAL check/switch and the ``CREATE TABLE IF NOT EXISTS``
statements run ONCE per process, lazy, loop-gated under an asyncio.Lock on
the async path and thread-locked on the sync path, never per operation.

Optimistic locking: ALL mutations go through
``UPDATE ... WHERE flow_id = ? AND expected_revision = ?``. When the update
matches zero rows the write conflicted (or the flow vanished); the caller
gets :class:`FlowConflictError` carrying the latest revision so it can
re-read (taskflow_summary) and retry, or :class:`FlowNotFoundError`.
"""

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from loguru import logger

from ..config import INITIAL_REVISION, TABLE_NAME, TaskFlowStatus

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "taskflow_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout, handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see ensure_db).
_INIT_WAIT_TIMEOUT_S = 10.0

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT
);
"""

_SELECT_COLUMNS_SQL = (
    f"SELECT flow_id, state_json, wait_json, expected_revision, status, child_session_key "
    f"FROM {TABLE_NAME}"
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
            f"but latest revision={latest_revision}; re-read with taskflow_summary and "
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
    flow_id, state_json, wait_json, expected_revision, status, child_session_key = row
    return {
        "flow_id": flow_id,
        "state": _load_json(state_json) or {},
        "wait": _load_json(wait_json),
        "expected_revision": int(expected_revision),
        "status": status,
        "child_session_key": child_session_key,
    }


@asynccontextmanager
async def _connect() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Open a short-lived connection; busy_timeout is always the FIRST statement."""
    db = await aiosqlite.connect(_DB_PATH)
    try:
        await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        yield db
    finally:
        await db.close()


async def _switch_to_wal_if_needed(db: aiosqlite.Connection) -> None:
    """Switch the database to WAL mode, unless it is already WAL.

    The journal-mode switch does NOT reliably honor busy_timeout: with an
    active writer it can raise OperationalError("database is locked")
    immediately. Once the file is WAL (the steady state after the first init)
    the pragma is skipped entirely, so later inits never contend on it. If a
    concurrent initializer is mid-switch, re-check and tolerate the outcome;
    init must never fail over the journal mode.
    """
    async with db.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
    mode = str(row[0]) if row and row[0] else ""
    if mode.lower() == "wal":
        return
    try:
        await db.execute("PRAGMA journal_mode=WAL")
    except aiosqlite.OperationalError:
        # Another connection may hold the exclusive lock for its own switch.
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        mode = str(row[0]) if row and row[0] else ""
        if mode.lower() != "wal":
            logger.warning(
                "taskflow registry db stays in {!r} journal mode (WAL switch contended); proceeding without WAL",
                mode,
            )


async def _init_db() -> None:
    """One-time schema setup; safe to run concurrently (busy_timeout + IF NOT EXISTS)."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        await _switch_to_wal_if_needed(db)
        await db.execute(_CREATE_TABLE_SQL)
        await db.commit()


async def ensure_db() -> None:
    """Ensure the database directory and required tables exist (once per process).

    Fast-returns once initialized. On first use the calling loop takes
    ownership and runs the schema init under its asyncio.Lock. Callers from
    other event loops never touch that lock (a foreign-thread release would
    wake a queued waiter via a non-threadsafe call_soon that can leave their
    loop asleep forever); they poll for the owner's one-time init and only
    self-init as a last resort, avoiding a rollback-journal stampede of
    concurrent DDL.
    """
    global _initialized, _init_loop
    if _initialized:
        return
    loop = asyncio.get_running_loop()
    if _init_loop is None:
        _init_loop = loop
    if _init_loop is loop:
        async with _init_lock:
            if _initialized:
                return
            await _init_db()
            _initialized = True
        return
    # Non-owning loop: wait for the owning loop to finish its one-time init.
    deadline = time.monotonic() + _INIT_WAIT_TIMEOUT_S
    while not _initialized and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if not _initialized:
        # Owning loop never finished (died mid-init): initialize ourselves.
        await _init_db()
        _initialized = True


def _ensure_tables_sync() -> None:
    """One-time table creation for the sync (stdlib sqlite3) paths.

    Thread-locked so concurrent sync callers cannot race on DDL; keeps the
    logged-and-swallowed contract of the sync read path.
    """
    global _sync_tables_ready
    if _sync_tables_ready:
        return
    with _sync_init_lock:
        if _sync_tables_ready:
            return
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()
        _sync_tables_ready = True


async def create_flow(
    flow_id: str,
    state: dict,
    *,
    status: str = TaskFlowStatus.RUNNING.value,
    child_session_key: str | None = None,
) -> dict:
    """Insert a new flow at INITIAL_REVISION; FlowExistsError on duplicate id."""
    flow_id = (flow_id or "").strip()
    if not flow_id:
        raise ValueError("flow_id must be a non-empty string")
    await ensure_db()
    try:
        async with _connect() as db:
            await db.execute(
                f"INSERT INTO {TABLE_NAME} "
                "(flow_id, state_json, wait_json, expected_revision, status, child_session_key) "
                "VALUES (?, ?, NULL, ?, ?, ?)",
                (flow_id, _dump_json(state), INITIAL_REVISION, status, child_session_key),
            )
            await db.commit()
    except aiosqlite.IntegrityError as e:
        raise FlowExistsError(flow_id) from e
    flow = await get_flow(flow_id)
    assert flow is not None  # we just inserted it
    return flow


async def get_flow(flow_id: str) -> dict | None:
    """Read one flow with parsed JSON columns; None when absent."""
    await ensure_db()
    async with _connect() as db:
        async with db.execute(
            _SELECT_COLUMNS_SQL + " WHERE flow_id = ?", (flow_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_flow(row)


async def update_flow(
    flow_id: str,
    expected_revision: int,
    *,
    state: dict | None = None,
    wait: dict | None | _Unset = UNSET,
    status: str | None = None,
    child_session_key: str | None | _Unset = UNSET,
) -> dict:
    """Optimistic-locking mutation of one flow row.

    Every call bumps ``expected_revision`` by exactly 1 and requires the row
    to still be at ``expected_revision``:

    * ``state``: replace state_json when not None.
    * ``wait``: replace wait_json; None clears it; UNSET keeps it.
    * ``status``: replace status when not None.
    * ``child_session_key``: replace when not UNSET.

    Returns the updated flow dict. Raises FlowNotFoundError when the flow does
    not exist, FlowConflictError (carrying the latest revision) when another
    writer won the race.
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
    if not assignments:
        raise ValueError("update_flow called with nothing to update")

    new_revision = int(expected_revision) + 1
    assignments.append("expected_revision = ?")
    params.append(new_revision)
    params.extend([flow_id, int(expected_revision)])

    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET {', '.join(assignments)} "
            "WHERE flow_id = ? AND expected_revision = ?",
            params,
        )
        if cursor.rowcount == 0:
            await db.rollback()
            async with db.execute(
                f"SELECT expected_revision FROM {TABLE_NAME} WHERE flow_id = ?",
                (flow_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise FlowNotFoundError(flow_id)
            raise FlowConflictError(flow_id, int(expected_revision), int(row[0]))
        await db.commit()

    flow = await get_flow(flow_id)
    assert flow is not None  # the UPDATE just matched this row
    return flow


def get_flow_sync(flow_id: str) -> dict | None:
    """Synchronously read one flow (stdlib sqlite3, no event loop required).

    Mirrors the subagent blueprint's sync paths: threading.Lock-guarded
    one-time table creation, connect-level busy timeout, failures logged and
    swallowed with a None return.
    """
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            row = conn.execute(
                _SELECT_COLUMNS_SQL + " WHERE flow_id = ?", (flow_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return _row_to_flow(row)
    except Exception as e:
        logger.warning("Failed to sync-read taskflow {}: {}", flow_id, e)
    return None
