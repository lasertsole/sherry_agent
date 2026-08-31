"""SQLite persistence layer: serializes SubagentRunRecord instances as JSON into aiosqlite.

Database path: subagent/data/subagent_registry.db
Table schema: subagent_runs(run_id TEXT PK, data TEXT) where data is model_dump_json()

Connection lifecycle: EVERY connection (aiosqlite and stdlib sqlite3 alike) is
configured with a 5s busy timeout as its first statement (or the equivalent
connect ``timeout``), so contended writes wait for the lock instead of failing
with ``sqlite3.OperationalError: database is locked``. The WAL check/switch and
the ``CREATE TABLE IF NOT EXISTS`` statements run ONCE per process — lazy,
loop-gated under an asyncio.Lock on the async path and thread-locked on the
sync path — never per operation, so concurrent writers cannot race on the
journal-mode switch or starve each other on DDL (the same contention shape
fixed for PendingInjectionStore in commit 608f4f6).
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

from ..types.registry import (
    SubagentRunRecord,
)

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "subagent_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout — handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see ensure_db).
_INIT_WAIT_TIMEOUT_S = 10.0

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS subagent_runs (
    run_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""

_CREATE_SETTLE_WAKE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS settle_wake_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
"""

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


def _serialize_run(run: SubagentRunRecord) -> str:
    """Serialize a run record to a JSON string."""
    return run.model_dump_json()


def _deserialize_run(data: str) -> SubagentRunRecord:
    """Deserialize a JSON string back into a SubagentRunRecord."""
    return SubagentRunRecord.model_validate_json(data)


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
    active writer it can raise OperationalError("database is locked") immediately.
    Once the file is WAL (the steady state after the first init) the pragma is
    skipped entirely, so later inits never contend on it. If a concurrent
    initializer is mid-switch, re-check and tolerate the outcome — init must
    never fail over the journal mode.
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
                "subagent registry db stays in {!r} journal mode (WAL switch contended); proceeding without WAL",
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

    Fast-returns once initialized. On first use the calling loop takes ownership
    and runs the schema init under its asyncio.Lock. Callers from other event
    loops never touch that lock (a foreign-thread release would wake a queued
    waiter via a non-threadsafe call_soon that can leave their loop asleep
    forever) — they poll for the owner's one-time init and only self-init as a
    last resort, avoiding a rollback-journal stampede of concurrent DDL.
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

    Called inside each sync function's existing try/except, so a failure keeps
    the logged-and-swallowed contract. Creates both tables so the once-only
    flag is shared safely regardless of which sync path runs first.
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
            conn.execute(_CREATE_SETTLE_WAKE_TABLE_SQL)
            conn.commit()
        finally:
            conn.close()
        _sync_tables_ready = True


async def save_runs_to_sqlite(runs: dict[str, SubagentRunRecord]) -> None:
    """Full-replace write of all run records (DELETE then INSERT)."""
    await ensure_db()
    async with _connect() as db:
        await db.execute("DELETE FROM subagent_runs")
        for run_id, run in runs.items():
            await db.execute(
                "INSERT INTO subagent_runs (run_id, data) VALUES (?, ?)",
                (run_id, _serialize_run(run)),
            )
        await db.commit()


async def load_runs_from_sqlite() -> dict[str, SubagentRunRecord]:
    """Load all run records from SQLite; records that fail deserialization are skipped."""
    await ensure_db()
    runs: dict[str, SubagentRunRecord] = {}
    try:
        async with _connect() as db:
            async with db.execute("SELECT run_id, data FROM subagent_runs") as cursor:
                async for row in cursor:
                    run_id, data = row
                    try:
                        runs[run_id] = _deserialize_run(data)
                    except Exception as e:
                        logger.warning("Failed to deserialize run {}: {}", run_id, e)
    except Exception as e:
        logger.warning("Failed to load from SQLite: {}", e)
    return runs


async def upsert_run_to_sqlite(run: SubagentRunRecord) -> None:
    """Upsert a single run record for incremental persistence."""
    await ensure_db()
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO subagent_runs (run_id, data) VALUES (?, ?)",
            (run.run_id, _serialize_run(run)),
        )
        await db.commit()


def upsert_run_sync(run: SubagentRunRecord) -> None:
    """Synchronously upsert a single run record.

    Mirrors ``save_settle_wake_state``: uses stdlib sqlite3 so that sync write
    paths (register_run / complete_run) can persist without a running event
    loop. Failures are logged and swallowed — memory remains the source of
    truth; SQLite is a best-effort restart-recovery mirror.
    """
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO subagent_runs (run_id, data) VALUES (?, ?)",
                (run.run_id, _serialize_run(run)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to sync-upsert run {} to SQLite: {}", run.run_id, e)


async def delete_run_from_sqlite(run_id: str) -> None:
    """Delete a single run record from SQLite by run_id."""
    await ensure_db()
    async with _connect() as db:
        await db.execute("DELETE FROM subagent_runs WHERE run_id = ?", (run_id,))
        await db.commit()


def save_settle_wake_state(state: dict) -> None:
    """Synchronously save settle-wake state to SQLite."""
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settle_wake_state (id, data) VALUES (1, ?)",
                (json.dumps(state),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Failed to save settle-wake state: {}", e)


def load_settle_wake_state() -> dict | None:
    """Synchronously load settle-wake state from SQLite."""
    try:
        _ensure_tables_sync()
        conn = sqlite3.connect(str(_DB_PATH), timeout=_BUSY_TIMEOUT_S)
        try:
            row = conn.execute("SELECT data FROM settle_wake_state WHERE id = 1").fetchone()
        finally:
            conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.debug("Failed to load settle-wake state: {}", e)
    return None
