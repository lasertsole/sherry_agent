"""Durable per-session FIFO user-input queue: SQLite-backed store (Task 1).

The crash-safe store behind "busy-time input queueing": while a session's turn
is running, newly submitted user input is persisted here and drained AFTER the
current turn finishes instead of cancelling it. This module is a PURE store --
no delivery decisions, no drain loops, no WS/channel frame sending (Tasks 5/7
own those), no priorities, no retries, no dead-letter handling.

The ``user_input_queue`` table lives in the SAME SQLite database file as
``PendingInjectionStore`` (``agent/tools/subagent/data/subagent_registry.db``);
it is additive -- the ``subagent_runs`` / ``settle_wake_state`` /
``pending_injections`` tables are never touched.

Connection lifecycle mirrors ``pending_injections.py``: EVERY aiosqlite
connection executes ``PRAGMA busy_timeout`` as its FIRST statement; WAL
check/switch and ``CREATE TABLE IF NOT EXISTS`` run ONCE per store instance
(lazy, lock-protected on first use). All mutating operations run inside a
single ``BEGIN IMMEDIATE`` transaction so the check-then-write sequences
(dedup, capacity, position) are atomic, and ``claim_next`` is race-free under
concurrency: 100 coroutines claiming the same session each get a DISTINCT row
(or None), never the same row twice.

Public API (consumed by Tasks 5/7/9/10 -- signatures are a contract):

- ``QueueFullError(Exception)`` -- raised by ``enqueue`` when a session already
  holds ``MAX_ACTIVE_PER_SESSION`` (20) active (QUEUED+CLAIMED) rows.
- ``UserInputQueueStatus`` (str Enum) -- ``QUEUED | CLAIMED | DELIVERED |
  FAILED | VOIDED``; only the last three are terminal.
- ``UserInputQueueRow`` (pydantic model) -- one persisted queue row.
- ``MAX_ACTIVE_PER_SESSION = 20`` -- per-session queue depth cap.
- ``UserInputQueue(db_path=None)``:
  - ``await enqueue(session_id, payload, source, reply_target=None,
    client_msg_id=None) -> tuple[UserInputQueueRow, int]``
    Atomic single transaction: (1) dedup -- an ACTIVE (QUEUED/CLAIMED) row with
    the same ``client_msg_id`` returns ``(existing_row, its_position)`` WITHOUT
    inserting; (2) capacity -- ``count_active >= 20`` raises ``QueueFullError``;
    (3) INSERT and return ``(row, position)`` where ``position`` is the new
    row's 1-based FIFO position among the session's QUEUED rows (the client
    facing "you are #N in line").
  - ``await claim_next(session_id) -> UserInputQueueRow | None``
    Atomically claims the OLDEST non-expired QUEUED row (``expires_at > now``):
    single ``UPDATE ... RETURNING`` guarded by ``BEGIN IMMEDIATE``, so
    concurrent claimers never double-claim. Returns the row as CLAIMED, or
    None when the queue is empty/fully expired.
  - ``await mark_terminal(row_id, status) -> None``
    Transitions an ACTIVE row to ``DELIVERED | FAILED | VOIDED`` (any other
    status raises ``ValueError``); unknown row ids are a no-op.
  - ``await count_active(session_id) -> int`` -- QUEUED + CLAIMED count.
  - ``await list_active(session_id) -> list[UserInputQueueRow]`` -- active rows
    ascending by ``created_at`` (FIFO).
  - ``await recover(session_id) -> int`` -- voids expired (``expires_at <= now``)
    QUEUED/CLAIMED rows (24h crash-recovery expiry), returns how many.
  - ``await find_active_by_client_msg_id(client_msg_id) -> UserInputQueueRow |
    None`` -- read-only ACTIVE-row lookup by the dedup key (Task 5 submit
    pre-check under its per-session lock; the enqueue/insert transactions
    remain the authoritative dedup).
  - ``await insert_claimed(session_id, payload, source, reply_target=None,
    client_msg_id=None) -> UserInputQueueRow`` -- insert a row ALREADY in
    CLAIMED state (Task 5 idle-branch placeholder: the durable "turn in
    progress" fact written in the same critical section as the idle check).
    Same capacity rule as ``enqueue`` (``QueueFullError`` at cap).

Row columns (``user_input_queue``): ``id TEXT PRIMARY KEY`` (uuid4 hex),
``session_id TEXT NOT NULL`` (indexed), ``payload TEXT NOT NULL`` (serialized
user-message JSON, isomorphic to the WS frame text/attachments payload),
``source TEXT NOT NULL`` ('user' | 'cron' -- persisted as a ROW COLUMN, never
inferred from payload at runtime), ``reply_target TEXT`` (channel routing JSON;
NULL on the WS path), ``client_msg_id TEXT`` (idempotency key: WS frame msg_id
or cron run_id; partial UNIQUE index over ACTIVE rows), ``status TEXT NOT
NULL``, ``created_at REAL NOT NULL`` (epoch seconds, FIFO sort key),
``updated_at REAL NOT NULL``, ``expires_at REAL NOT NULL`` (created_at + 24h).
"""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Literal

import aiosqlite
from loguru import logger
from pydantic import BaseModel

# The new table lives in the SAME SQLite database file the PendingInjectionStore
# uses (agent/tools/subagent/data/subagent_registry.db) -- additive only.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _REPO_ROOT / "agent" / "tools" / "subagent" / "data" / "subagent_registry.db"

# Wait (up to) this long for a contended SQLite lock on EVERY connection.
_BUSY_TIMEOUT_MS = 5000

# How long a non-owning event loop waits for the owning loop's one-time schema
# init before initializing the schema itself (see _ensure_db).
_INIT_WAIT_TIMEOUT_S = 10.0

# Per-session queue depth cap: QUEUED + CLAIMED rows.
MAX_ACTIVE_PER_SESSION = 20

# Crash-recovery expiry: rows older than this are voided by recover().
_EXPIRY_SECONDS = 24 * 60 * 60.0

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_input_queue (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    source TEXT NOT NULL,
    reply_target TEXT,
    client_msg_id TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

_CREATE_INDEX_SQLS = (
    # FIFO scans are per-session.
    "CREATE INDEX IF NOT EXISTS idx_user_input_queue_session "
    "ON user_input_queue (session_id)",
    # Idempotency: at most one ACTIVE row per client_msg_id (dedup key).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_input_queue_client_msg_active "
    "ON user_input_queue (client_msg_id) "
    "WHERE client_msg_id IS NOT NULL AND status IN ('QUEUED', 'CLAIMED')",
)

_ACTIVE_STATUSES_SQL = "('QUEUED', 'CLAIMED')"

_ROW_COLUMNS = (
    "id, session_id, payload, source, reply_target, client_msg_id, "
    "status, created_at, updated_at, expires_at"
)

# Atomic race-free claim: the subquery and the UPDATE run as ONE statement under
# the write lock, so two concurrent claimers can never select the same row.
_CLAIM_NEXT_SQL = f"""
UPDATE user_input_queue
SET status = 'CLAIMED', updated_at = ?
WHERE id = (
    SELECT id FROM user_input_queue
    WHERE session_id = ? AND status = 'QUEUED' AND expires_at > ?
    ORDER BY created_at ASC, id ASC
    LIMIT 1
)
RETURNING {_ROW_COLUMNS};
"""

_TERMINAL_STATUSES_SQL = "('DELIVERED', 'FAILED', 'VOIDED')"

_MARK_TERMINAL_SQL = f"""
UPDATE user_input_queue
SET status = ?, updated_at = ?
WHERE id = ? AND status IN {_ACTIVE_STATUSES_SQL}
RETURNING {_ROW_COLUMNS};
"""

_RECOVER_SQL = f"""
UPDATE user_input_queue
SET status = 'VOIDED', updated_at = ?
WHERE session_id = ? AND status IN {_ACTIVE_STATUSES_SQL} AND expires_at <= ?;
"""

# Task 5 helper: read-only ACTIVE-row lookup by the dedup key.
_FIND_ACTIVE_BY_CLIENT_MSG_SQL = f"""
SELECT {_ROW_COLUMNS} FROM user_input_queue
WHERE client_msg_id = ? AND status IN {_ACTIVE_STATUSES_SQL}
ORDER BY created_at ASC, id ASC
LIMIT 1;
"""

# Task 5 idle-branch placeholder: a row that starts life CLAIMED ("turn in
# progress" fact), inserted in the same critical section as the idle check.
_INSERT_CLAIMED_SQL = """
INSERT INTO user_input_queue
(id, session_id, payload, source, reply_target, client_msg_id, status, created_at, updated_at, expires_at)
VALUES (?, ?, ?, ?, ?, ?, 'CLAIMED', ?, ?, ?);
"""


async def _switch_to_wal_if_needed(db: aiosqlite.Connection) -> None:
    """Switch the database to WAL mode, unless it is already WAL.

    The journal-mode switch does NOT reliably honor busy_timeout: with an
    active writer it can raise OperationalError("database is locked") immediately.
    Once the file is WAL (the steady state after the first init) the pragma is
    skipped entirely. If a concurrent initializer is mid-switch, re-check and
    tolerate the outcome -- init must never fail over the journal mode.
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
                "user_input_queue db stays in {!r} journal mode (WAL switch contended); proceeding without WAL",
                mode,
            )


class QueueFullError(Exception):
    """A session's user-input queue is at capacity (MAX_ACTIVE_PER_SESSION)."""


class UserInputQueueStatus(str, Enum):
    """Lifecycle of a queued user input.

    QUEUED -> CLAIMED -> DELIVERED (drained into a turn)
                       -> FAILED    (turn dispatch error; log + continue)
                       -> VOIDED    (expired via recover() or superseded)
    QUEUED and CLAIMED are the ACTIVE statuses; the rest are terminal.
    """

    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    VOIDED = "VOIDED"


_TERMINAL_STATUSES = frozenset(
    {UserInputQueueStatus.DELIVERED, UserInputQueueStatus.FAILED, UserInputQueueStatus.VOIDED}
)


class UserInputQueueRow(BaseModel):
    """One persisted queued user input.

    ``source`` is fixed at enqueue time as a row column -- classification must
    never be re-inferred from the payload at runtime (rehydrate lesson from
    steering_queue.py:95-109). ``payload`` is the serialized user-message JSON;
    ``reply_target`` carries the channel routing JSON (None on the WS path).
    """

    id: str
    session_id: str
    payload: str
    source: Literal["user", "cron"]
    reply_target: str | None = None
    client_msg_id: str | None = None
    status: UserInputQueueStatus = UserInputQueueStatus.QUEUED
    created_at: float
    updated_at: float
    expires_at: float


def _row_from_db(row: aiosqlite.Row) -> UserInputQueueRow:
    """Build a UserInputQueueRow from a SELECT/RETURNING row tuple."""
    return UserInputQueueRow(
        id=row[0],
        session_id=row[1],
        payload=row[2],
        source=row[3],
        reply_target=row[4],
        client_msg_id=row[5],
        status=UserInputQueueStatus(row[6]),
        created_at=row[7],
        updated_at=row[8],
        expires_at=row[9],
    )


class UserInputQueue:
    """Async store for queued user inputs, persisted via aiosqlite (WAL).

    Open one instance per process (or per test) pointed at the same db file;
    state survives across instances ("restarts") because SQLite is the source
    of truth for this queue.

    Connections are short-lived and every one of them sets ``PRAGMA
    busy_timeout`` as its first statement. Schema init (WAL check/switch +
    CREATE TABLE/INDEX) runs at most once per instance, on first use,
    serialized by an asyncio lock on the instance's owning loop. Every mutating
    operation runs in a single ``BEGIN IMMEDIATE`` transaction: the write lock
    is acquired up front, so the multi-statement check-then-write sequences
    cannot interleave with a concurrent writer (race-free enqueue dedup/cap
    checks and claim).

    Same-loop writes are additionally serialized by a per-instance
    ``asyncio.Lock`` (``_write_lock``): SQLite's busy-handler backoff is UNFAIR
    (sleeps up to 100 ms between retries), so under heavy same-loop contention
    (e.g. 100 concurrent claims) individual ``BEGIN IMMEDIATE`` attempts can
    exceed ``busy_timeout`` before ever acquiring the SQLite write lock and
    fail with "database is locked". The in-process lock removes that contention
    entirely (the deployment is single-process), while ``BEGIN IMMEDIATE`` +
    ``busy_timeout`` still guard cross-instance / cross-process atomicity --
    claim atomicity itself rests on the single ``UPDATE ... RETURNING``
    statement, which is correct with or without the lock.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._db_dir = self._db_path.parent
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._init_loop: asyncio.AbstractEventLoop | None = None
        self._initialized: bool = False

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Open a short-lived connection; busy_timeout is always the FIRST statement."""
        db = await aiosqlite.connect(self._db_path)
        try:
            await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield db
        finally:
            await db.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """One connection + one BEGIN IMMEDIATE transaction, committed on success.

        An exception inside the block rolls the transaction back before the
        connection closes, so partial writes never survive.
        """
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Serialize same-loop writers, then run one BEGIN IMMEDIATE transaction.

        The per-instance lock keeps SQLite-level write-lock contention (and its
        unfair busy-handler backoff) out of the single-process deployment; the
        transaction still guarantees atomicity if a foreign writer contends.
        """
        async with self._write_lock:
            async with self._transaction() as db:
                yield db

    async def _init_db(self) -> None:
        """One-time schema setup; safe to run concurrently (busy_timeout + IF NOT EXISTS)."""
        self._db_dir.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await _switch_to_wal_if_needed(db)
            await db.execute(_CREATE_TABLE_SQL)
            for index_sql in _CREATE_INDEX_SQLS:
                await db.execute(index_sql)
            await db.commit()

    async def _ensure_db(self) -> None:
        """Ensure the database directory and the user_input_queue table exist (once per instance)."""
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
        # primitives are single-loop by design -- wait for the owning loop to
        # finish its one-time init instead of touching the lock.
        deadline = time.monotonic() + _INIT_WAIT_TIMEOUT_S
        while not self._initialized and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not self._initialized:
            await self._init_db()
            self._initialized = True

    async def enqueue(
        self,
        session_id: str,
        payload: str,
        source: Literal["user", "cron"],
        reply_target: str | None = None,
        client_msg_id: str | None = None,
    ) -> tuple[UserInputQueueRow, int]:
        """Append a user input to a session's FIFO queue (single atomic transaction).

        Order of operations inside one ``BEGIN IMMEDIATE`` transaction:
        1. Dedup: an ACTIVE (QUEUED/CLAIMED) row with the same ``client_msg_id``
           (any session) -> return ``(existing_row, position)`` unchanged, no
           INSERT. Terminal rows do not dedup (the key is free again).
        2. Capacity: ``count_active(session_id) >= MAX_ACTIVE_PER_SESSION`` ->
           raise ``QueueFullError``; nothing is inserted.
        3. INSERT (status=QUEUED, expires_at=created_at+24h) and return
           ``(row, position)`` -- ``position`` is the row's 1-based FIFO rank
           among the session's QUEUED rows (1 == next to be claimed).
        """
        if source not in ("user", "cron"):
            raise ValueError(f"source must be 'user' or 'cron', got {source!r}")

        await self._ensure_db()
        now = time.time()
        row_id = uuid.uuid4().hex

        async with self._write_transaction() as db:
            # 1. Dedup against ACTIVE rows with the same client_msg_id.
            if client_msg_id is not None:
                async with db.execute(
                    "SELECT * FROM user_input_queue WHERE client_msg_id = ? "
                    f"AND status IN {_ACTIVE_STATUSES_SQL} ORDER BY created_at ASC LIMIT 1",
                    (client_msg_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    existing_row = _row_from_db(existing)
                    position = await self._queued_position(db, existing_row)
                    logger.debug(
                        "user_input_queue dedup: client_msg_id {} already active as row {} (position {}); enqueue is a no-op",
                        client_msg_id,
                        existing_row.id,
                        position,
                    )
                    return existing_row, position

            # 2. Capacity: QUEUED + CLAIMED for this session.
            async with db.execute(
                f"SELECT COUNT(*) FROM user_input_queue WHERE session_id = ? AND status IN {_ACTIVE_STATUSES_SQL}",
                (session_id,),
            ) as cursor:
                count_row = await cursor.fetchone()
                assert count_row is not None, "COUNT(*) always returns a row"
                (active_count,) = count_row
            if active_count >= MAX_ACTIVE_PER_SESSION:
                raise QueueFullError(
                    f"input queue full for session {session_id!r}: "
                    f"{active_count} active rows >= cap {MAX_ACTIVE_PER_SESSION}"
                )

            # 3. Insert and compute the new row's FIFO position.
            created_at = now
            expires_at = created_at + _EXPIRY_SECONDS
            await db.execute(
                "INSERT INTO user_input_queue "
                "(id, session_id, payload, source, reply_target, client_msg_id, status, created_at, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                (
                    row_id,
                    session_id,
                    payload,
                    source,
                    reply_target,
                    client_msg_id,
                    created_at,
                    now,
                    expires_at,
                ),
            )
            async with db.execute(
                "SELECT * FROM user_input_queue WHERE id = ?", (row_id,)
            ) as cursor:
                inserted = await cursor.fetchone()
            assert inserted is not None, "inserted row vanished inside its own transaction"
            row = _row_from_db(inserted)
            position = await self._queued_position(db, row)
            return row, position

    @staticmethod
    async def _queued_position(db: aiosqlite.Connection, row: UserInputQueueRow) -> int:
        """1-based FIFO rank of ``row`` among its session's QUEUED rows.

        For a CLAIMED row there is no queue slot: returns 0 (already being
        processed, not waiting in line).
        """
        if row.status is not UserInputQueueStatus.QUEUED:
            return 0
        async with db.execute(
            "SELECT COUNT(*) FROM user_input_queue "
            "WHERE session_id = ? AND status = 'QUEUED' "
            "AND (created_at < ? OR (created_at = ? AND id <= ?))",
            (row.session_id, row.created_at, row.created_at, row.id),
        ) as cursor:
            position_row = await cursor.fetchone()
            assert position_row is not None, "COUNT(*) always returns a row"
            (position,) = position_row
        return int(position)

    async def claim_next(self, session_id: str) -> UserInputQueueRow | None:
        """Atomically claim the OLDEST non-expired QUEUED row of ``session_id``.

        Race-free under concurrency: a single ``UPDATE ... RETURNING`` (guarded
        by ``BEGIN IMMEDIATE``) both selects and flips the row to CLAIMED, so
        among N concurrent claimers each row is returned exactly once -- every
        caller gets a DISTINCT row or None. Expired rows (``expires_at <= now``)
        are skipped; call ``recover`` to void them.
        """
        await self._ensure_db()
        now = time.time()
        async with self._write_transaction() as db:
            cursor = await db.execute(_CLAIM_NEXT_SQL, (now, session_id, now))
            claimed = await cursor.fetchone()
            if claimed is None:
                return None
            return _row_from_db(claimed)

    async def mark_terminal(
        self, row_id: str, status: UserInputQueueStatus | Literal["DELIVERED", "FAILED", "VOIDED"]
    ) -> None:
        """Transition an ACTIVE row to a terminal status (DELIVERED | FAILED | VOIDED).

        ``status`` accepts the enum value or its plain string. Any non-terminal
        status (e.g. QUEUED/CLAIMED) raises ``ValueError``. Unknown row ids (or
        rows already terminal) are a no-op -- terminal states are final.
        """
        terminal = UserInputQueueStatus(status)
        if terminal not in _TERMINAL_STATUSES:
            allowed = " | ".join(s.value for s in sorted(_TERMINAL_STATUSES, key=lambda x: x.value))
            raise ValueError(
                f"mark_terminal requires a terminal status ({allowed}), got {terminal.value!r}"
            )

        await self._ensure_db()
        async with self._write_transaction() as db:
            cursor = await db.execute(_MARK_TERMINAL_SQL, (terminal.value, time.time(), row_id))
            if await cursor.fetchone() is None:
                logger.debug(
                    "user_input_queue mark_terminal: row {} not active (unknown id or already terminal); no-op",
                    row_id,
                )

    async def count_active(self, session_id: str) -> int:
        """Number of ACTIVE (QUEUED + CLAIMED) rows for ``session_id``."""
        await self._ensure_db()
        async with self._connect() as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM user_input_queue WHERE session_id = ? AND status IN {_ACTIVE_STATUSES_SQL}",
                (session_id,),
            ) as cursor:
                count_row = await cursor.fetchone()
                assert count_row is not None, "COUNT(*) always returns a row"
                (count,) = count_row
        return int(count)

    async def list_active(self, session_id: str) -> list[UserInputQueueRow]:
        """All ACTIVE (QUEUED + CLAIMED) rows for ``session_id``, FIFO order (created_at ASC)."""
        await self._ensure_db()
        rows: list[UserInputQueueRow] = []
        async with self._connect() as db:
            async with db.execute(
                f"SELECT {_ROW_COLUMNS} FROM user_input_queue "
                f"WHERE session_id = ? AND status IN {_ACTIVE_STATUSES_SQL} "
                "ORDER BY created_at ASC, id ASC",
                (session_id,),
            ) as cursor:
                async for row in cursor:
                    rows.append(_row_from_db(row))
        return rows

    async def recover(self, session_id: str) -> int:
        """Void expired QUEUED/CLAIMED rows (24h crash-recovery expiry); return the count.

        Rows whose ``expires_at <= now`` are flipped to VOIDED -- they are never
        delivered and no longer count as active. Fresh rows are untouched.
        """
        await self._ensure_db()
        now = time.time()
        async with self._write_transaction() as db:
            cursor = await db.execute(_RECOVER_SQL, (now, session_id, now))
            voided = cursor.rowcount
        if voided > 0:
            logger.info(
                "user_input_queue recover: voided {} expired row(s) for session {}",
                voided,
                session_id,
            )
        return int(voided)

    async def find_active_by_client_msg_id(self, client_msg_id: str) -> UserInputQueueRow | None:
        """Return the ACTIVE (QUEUED/CLAIMED) row carrying ``client_msg_id``, or None.

        Read-only dedup lookup used by ``submit_user_input`` (Task 5) to
        classify a repeated ``client_msg_id`` as DEDUPED before any insert or
        turn dispatch. The authoritative dedup remains inside the
        ``enqueue``/``insert_claimed`` transactions (partial UNIQUE index);
        this helper is the pre-check under the caller's per-session lock.
        """
        await self._ensure_db()
        async with self._connect() as db:
            async with db.execute(
                _FIND_ACTIVE_BY_CLIENT_MSG_SQL, (client_msg_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return _row_from_db(row) if row is not None else None

    async def insert_claimed(
        self,
        session_id: str,
        payload: str,
        source: Literal["user", "cron"],
        reply_target: str | None = None,
        client_msg_id: str | None = None,
    ) -> UserInputQueueRow:
        """Insert a row ALREADY in CLAIMED state (Task 5 idle-branch placeholder).

        ``submit_user_input`` writes this row inside its per-session lock, in
        the same critical section as the idle check: the row is the durable
        "turn in progress" fact that makes any racing submit see the session
        as busy and enqueue instead of double-dispatching a turn. Task 7's
        drain consumes CLAIMED rows through the same lifecycle machinery
        (``mark_terminal``) as QUEUED ones; ``claim_next`` never returns them.

        Single ``BEGIN IMMEDIATE`` transaction: the capacity check
        (``count_active >= MAX_ACTIVE_PER_SESSION`` -> ``QueueFullError``) and
        the INSERT are atomic, mirroring ``enqueue``. Dedup is NOT re-checked
        here -- callers pre-check via ``find_active_by_client_msg_id`` under
        their own lock; the partial UNIQUE index stays the cross-instance
        backstop (an IntegrityError from a foreign duplicate propagates).
        """
        if source not in ("user", "cron"):
            raise ValueError(f"source must be 'user' or 'cron', got {source!r}")

        await self._ensure_db()
        now = time.time()
        row_id = uuid.uuid4().hex
        created_at = now
        expires_at = created_at + _EXPIRY_SECONDS

        async with self._write_transaction() as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM user_input_queue WHERE session_id = ? AND status IN {_ACTIVE_STATUSES_SQL}",
                (session_id,),
            ) as cursor:
                count_row = await cursor.fetchone()
                assert count_row is not None, "COUNT(*) always returns a row"
                (active_count,) = count_row
            if active_count >= MAX_ACTIVE_PER_SESSION:
                raise QueueFullError(
                    f"input queue full for session {session_id!r}: "
                    f"{active_count} active rows >= cap {MAX_ACTIVE_PER_SESSION}"
                )
            await db.execute(
                _INSERT_CLAIMED_SQL,
                (
                    row_id,
                    session_id,
                    payload,
                    source,
                    reply_target,
                    client_msg_id,
                    created_at,
                    now,
                    expires_at,
                ),
            )
            async with db.execute(
                "SELECT * FROM user_input_queue WHERE id = ?", (row_id,)
            ) as cursor:
                inserted = await cursor.fetchone()
            assert inserted is not None, "inserted row vanished inside its own transaction"
            return _row_from_db(inserted)
