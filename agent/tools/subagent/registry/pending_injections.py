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
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Literal

import aiosqlite
from loguru import logger
from pydantic import BaseModel, Field

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "subagent_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection. The
# journal-mode switch is the one operation that does not reliably honor this
# timeout — handled separately in _switch_to_wal_if_needed.
_BUSY_TIMEOUT_MS = 5000

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see _ensure_db).
_INIT_WAIT_TIMEOUT_S = 10.0

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_injections (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

# Atomic single-statement transition used by mark_consumed: the status guard in
# WHERE makes concurrent callers race-safe (exactly one row changes per run_id).
_MARK_CONSUMED_SQL = """
UPDATE pending_injections
SET status = 'consumed', data = json_set(data, '$.status', 'consumed')
WHERE run_id = ? AND status = 'pending';
"""


async def _switch_to_wal_if_needed(db: aiosqlite.Connection) -> None:
    """Switch the database to WAL mode, unless it is already WAL.

    The journal-mode switch does NOT reliably honor busy_timeout: with an
    active writer it can raise OperationalError("database is locked") immediately.
    Once the file is WAL (the steady state after the first init) the pragma is
    skipped entirely, so later per-instance inits never contend on it. If a
    concurrent initializer is mid-switch, re-check and tolerate the outcome —
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
                "pending_injections db stays in {!r} journal mode (WAL switch contended); proceeding without WAL",
                mode,
            )


class PendingInjectionStatus(str, Enum):
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


class PendingInjectionStore:
    """Async store for pending injections, persisted via aiosqlite (WAL).

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
        self._init_lock = asyncio.Lock()
        self._init_loop: asyncio.AbstractEventLoop | None = None
        self._initialized = False

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Open a short-lived connection; busy_timeout is always the FIRST statement."""
        db = await aiosqlite.connect(self._db_path)
        try:
            await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield db
        finally:
            await db.close()

    async def _init_db(self) -> None:
        """One-time schema setup; safe to run concurrently (busy_timeout + IF NOT EXISTS)."""
        self._db_dir.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await _switch_to_wal_if_needed(db)
            await db.execute(_CREATE_TABLE_SQL)
            await db.commit()

    async def _ensure_db(self) -> None:
        """Ensure the database directory and the pending_injections table exist (once per instance)."""
        if self._initialized:
            return
        loop = asyncio.get_running_loop()
        if self._init_loop is None:
            self._init_loop = loop
        if self._init_loop is loop:
            async with self._init_lock:
                if self._initialized:
                    return
                await self._init_db()
                self._initialized = True
            return
        # Non-owning loop (a store instance driven from more than one event
        # loop, e.g. test harnesses mixing threads and asyncio). asyncio
        # primitives are single-loop by design — a release from a foreign
        # thread would wake a queued waiter via a non-threadsafe call_soon that
        # can leave that waiter's loop asleep forever — so instead of touching
        # the lock, wait for the owning loop to finish its one-time init.
        # Stampeding the database with concurrent CREATE TABLEs from several
        # loops must be avoided: in rollback-journal mode (before the WAL
        # switch lands) concurrent writers starve each other's busy timeouts.
        deadline = time.monotonic() + _INIT_WAIT_TIMEOUT_S
        while not self._initialized and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not self._initialized:
            # Owning loop never finished (died mid-init): initialize ourselves.
            await self._init_db()
            self._initialized = True

    async def enqueue(self, injection: PendingInjection) -> PendingInjection:
        """Insert a new pending injection; idempotent on run_id.

        If a record for the same run_id already exists (any status), the insert is
        a no-op and the EXISTING record is returned — duplicates never overwrite,
        and consumed records are never revived.
        """
        await self._ensure_db()
        payload = injection.model_dump_json()
        async with self._connect() as db:
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
        await self._ensure_db()
        async with self._connect() as db:
            cursor = await db.execute(_MARK_CONSUMED_SQL, (run_id,))
            await db.commit()
            if cursor.rowcount == 1:
                logger.debug("Pending injection for run {} marked consumed", run_id)
                return True
        return False

    async def list_pending(self) -> list[PendingInjection]:
        """List all PENDING injections, oldest first (by created_at, then insertion order)."""
        await self._ensure_db()
        records: list[PendingInjection] = []
        async with self._connect() as db:
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
        await self._ensure_db()
        async with self._connect() as db:
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
