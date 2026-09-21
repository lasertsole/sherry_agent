"""SQLite persistence layer: serializes SubagentRunRecord instances as JSON into aiosqlite.

Database path: subagent/data/subagent_registry.db
Table schema: subagent_runs(run_id TEXT PK, data TEXT) where data is model_dump_json()

Connection lifecycle comes from ``agent.tools.pub_base.sqlite_store``
(``BaseSQLiteRepository``, the shared skeleton also used by the todolist and
taskflow registries): EVERY connection (aiosqlite and stdlib sqlite3 alike) is
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
from pathlib import Path

from loguru import logger

from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository
from config.features import SUBAGENT_INFRA
from ..types.registry import (
    SubagentRunRecord,
)

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "subagent_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout — handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = SUBAGENT_INFRA["registry_store_busy_timeout_ms"]
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000.0

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see ensure_db).
_INIT_WAIT_TIMEOUT_S = SUBAGENT_INFRA["registry_init_wait_timeout_s"]

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


class _SubagentRegistryRepository(BaseSQLiteRepository):
    """Base-class wiring for the subagent_runs / settle_wake_state store."""

    wal_label = "subagent registry db"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_CREATE_TABLE_SQL,)

    def _sync_table_ddls(self) -> tuple[str, ...]:
        return (_CREATE_TABLE_SQL, _CREATE_SETTLE_WAKE_TABLE_SQL)


# The base reads/writes the module-level init state through this namespace, so
# the existing monkeypatch contract (``_DB_PATH``/``_initialized``/...) is intact.
_repository = _SubagentRegistryRepository(globals())

_connect = _repository.connect
_switch_to_wal_if_needed = _repository.switch_to_wal_if_needed
ensure_db = _repository.ensure_db
_ensure_tables_sync = _repository.ensure_tables_sync


async def save_runs_to_sqlite(runs: dict[str, SubagentRunRecord]) -> None:
    """Full-sync write: incrementally upsert all run records, then delete run_ids absent from the snapshot."""
    await ensure_db()
    async with _connect() as db:
        for run_id, run in runs.items():
            await db.execute(
                "INSERT OR REPLACE INTO subagent_runs (run_id, data) VALUES (?, ?)",
                (run_id, _serialize_run(run)),
            )
        if runs:
            placeholders = ",".join("?" for _ in runs)
            await db.execute(
                f"DELETE FROM subagent_runs WHERE run_id NOT IN ({placeholders})",
                tuple(runs.keys()),
            )
        else:
            await db.execute("DELETE FROM subagent_runs")
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
