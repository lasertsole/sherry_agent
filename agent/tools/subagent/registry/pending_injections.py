"""Durable pending-injection queue: subagent-completion injection records persisted to SQLite.

The crash-safe store behind both delivery paths (busy steering / idle auto-turn).
Records are idempotent on ``run_id`` — duplicate enqueues are no-ops returning the
existing row — and transition PENDING → CONSUMED exactly once (race-safe
``mark_consumed`` for concurrent delivery paths).

Pure store: no delivery decisions, no busy/idle logic, no auto-turn triggers.

Table: ``pending_injections(run_id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL)``
where ``data`` is ``PendingInjection.model_dump_json()``. The table lives in the
subagent registry database (``subagent/data/subagent_registry.db``) and is created
via the same aiosqlite infrastructure as ``store_sqlite.py`` — additive only; the
``subagent_runs`` / ``settle_wake_state`` tables are never touched.

Connection lifecycle: EVERY aiosqlite connection (including the one-time schema
init) executes ``PRAGMA busy_timeout`` as its FIRST statement, so contended
writes wait for the lock instead of failing. The WAL check/switch and the
``CREATE TABLE IF NOT EXISTS`` run ONCE per store instance (lazy, lock-protected
on first use) — never per operation — so concurrent same-loop enqueues cannot
race on the journal-mode switch or trip "database is locked".
"""

import asyncio
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from config.features import SUBAGENT_INFRA
from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "subagent_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout — handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = SUBAGENT_INFRA["pending_injections_busy_timeout_ms"]

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see _ensure_db).
_INIT_WAIT_TIMEOUT_S = SUBAGENT_INFRA["registry_init_wait_timeout_s"]

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_injections (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

_CREATE_STATUS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pending_injections_status
ON pending_injections(status)
WHERE status = 'pending';
"""

# Atomic single-statement transition used by mark_consumed: the status guard in
# WHERE makes concurrent callers race-safe (exactly one row changes per run_id).
_MARK_CONSUMED_SQL = """
UPDATE pending_injections
SET status = 'consumed', data = json_set(data, '$.status', 'consumed')
WHERE run_id = ? AND status = 'pending';
"""


class PendingInjectionStatus(StrEnum):
    """Lifecycle of an injection record: queued → delivered/consumed by a delivery path."""

    PENDING = "pending"
    CONSUMED = "consumed"


class PendingInjection(BaseModel):
    """One queued subagent-completion message awaiting injection into a parent turn.

    ``run_id`` doubles as the idempotency key: at most one injection record exists
    per subagent run. ``provenance`` is fixed so downstream consumers can classify
    the injected message without heuristics.
    """

    run_id: str
    requester_session_key: str
    child_session_key: str | None = None
    child_agent_id: str | None = None
    child_name: str | None = None
    content: str
    status: PendingInjectionStatus = PendingInjectionStatus.PENDING
    provenance: Literal["subagent_completion"] = "subagent_completion"
    created_at: float = Field(default_factory=time.time)


class PendingInjectionStore(BaseSQLiteRepository):
    """Async store for pending injections, persisted via aiosqlite (WAL).

    The connection lifecycle and the once-per-instance schema init come from
    ``BaseSQLiteRepository`` (the same skeleton the todo/taskflow/registry
    stores use) — this class used to copy them. Its namespace is PER INSTANCE
    (unlike the module-level stores), because a caller may point it at its own
    ``db_path`` (tests); the isolation contract is unchanged.

    Open one instance per process (or per test) pointed at the same db file;
    state survives across instances ("restarts") because SQLite is the source
    of truth for this queue.

    Connections are short-lived and every one of them sets ``PRAGMA
    busy_timeout`` as its first statement. Schema init (WAL check/switch +
    CREATE TABLE) runs at most once per instance, on first use, serialized by
    an asyncio lock on the instance's owning loop — concurrent callers never
    re-execute the journal-mode switch or the DDL, which is what keeps
    same-loop enqueue bursts loss-free. Callers from other event loops (test
    harnesses) wait for that same init instead of racing their own.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._db_dir = self._db_path.parent
        # The base reads/writes its init state through this namespace; keeping
        # it per instance preserves the ``db_path=`` isolation the tests use.
        self._ns: dict[str, Any] = {
            "_DB_DIR": self._db_dir,
            "_DB_PATH": self._db_path,
            "_BUSY_TIMEOUT_MS": _BUSY_TIMEOUT_MS,
            "_BUSY_TIMEOUT_S": _BUSY_TIMEOUT_MS / 1000,
            "_INIT_WAIT_TIMEOUT_S": _INIT_WAIT_TIMEOUT_S,
            "_initialized": False,
            "_init_loop": None,
            "_init_lock": asyncio.Lock(),
            "_sync_tables_ready": False,
            "_sync_init_lock": threading.Lock(),
        }
        super().__init__(self._ns)

    # -- schema hooks (BaseSQLiteRepository) -------------------------------

    def _table_ddls(self) -> tuple[str, ...]:
        return (_CREATE_TABLE_SQL,)

    def _index_ddls(self) -> tuple[str, ...]:
        return (_CREATE_STATUS_INDEX_SQL,)

    async def enqueue(self, injection: PendingInjection) -> PendingInjection:
        """Insert a new pending injection; idempotent on run_id.

        If a record for the same run_id already exists (any status), the insert is
        a no-op and the EXISTING record is returned — duplicates never overwrite,
        and consumed records are never revived.
        """
        await self.ensure_db()
        payload = injection.model_dump_json()
        async with self.connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO pending_injections (run_id, status, data) VALUES (?, ?, ?)",
                (injection.run_id, injection.status.value, payload),
            )
            await db.commit()
            inserted = cursor.rowcount == 1
            async with db.execute(
                "SELECT data FROM pending_injections WHERE run_id = ?",
                (injection.run_id,),
            ) as select_cursor:
                row = await select_cursor.fetchone()
        if not inserted:
            logger.debug(
                "Pending injection for run {} already exists; enqueue is a no-op",
                injection.run_id,
            )
        if row is None:
            raise RuntimeError(
                f"pending_injections row for run {injection.run_id} vanished after enqueue"
            )
        return PendingInjection.model_validate_json(row[0])

    async def mark_consumed(self, run_id: str) -> bool:
        """Transition a pending record to CONSUMED exactly once.

        Returns True if THIS call performed the transition; False when the run_id
        is unknown or already consumed. Safe to call concurrently from competing
        delivery paths — the status guard in the UPDATE ensures a single winner.
        """
        await self.ensure_db()
        async with self.connect() as db:
            cursor = await db.execute(_MARK_CONSUMED_SQL, (run_id,))
            await db.commit()
            if cursor.rowcount == 1:
                logger.debug("Pending injection for run {} marked consumed", run_id)
                return True
        return False

    async def list_pending(self) -> list[PendingInjection]:
        """List all PENDING injections, oldest first (by created_at, then insertion order)."""
        await self.ensure_db()
        records: list[PendingInjection] = []
        async with self.connect() as db:
            async with db.execute(
                "SELECT run_id, data FROM pending_injections WHERE status = 'pending' ORDER BY rowid"
            ) as cursor:
                async for row in cursor:
                    run_id, data = row
                    try:
                        records.append(PendingInjection.model_validate_json(data))
                    except Exception as e:
                        logger.warning("Failed to deserialize pending injection {}: {}", run_id, e)
        records.sort(key=lambda r: r.created_at)
        return records

    async def get(self, run_id: str) -> PendingInjection | None:
        """Fetch a single injection record by run_id (any status); None when absent."""
        await self.ensure_db()
        async with self.connect() as db:
            async with db.execute(
                "SELECT data FROM pending_injections WHERE run_id = ?", (run_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return PendingInjection.model_validate_json(row[0])
        except Exception as e:
            logger.warning("Failed to deserialize pending injection {}: {}", run_id, e)
            return None
