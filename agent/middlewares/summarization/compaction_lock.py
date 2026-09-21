"""SQLite-backed compaction lock (from hermes-agent).

Prevents two concurrent compactions of the same session from splitting the
session state (multi-instance deployments, concurrent API requests). The
lock lives in the MesMemory SQLite database: the PRIMARY KEY on session_id
makes concurrent acquire attempts mutually exclusive at the storage level,
and a TTL bounds the damage of a crashed holder.

Simplified variant without a renewal thread: TTL 300 s, expired locks are
purged on the next acquire attempt. Sufficient for single-instance
deployments; multi-instance renewals can be added later.

Cross-platform: pathlib paths, per-call sqlite3 connections (WAL-safe), no
POSIX-only primitives. The database path is resolved lazily at connect time
so test/eval redirection of ``context_engine.store.db._db_path`` is honored.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from loguru import logger

_DEFAULT_TTL_S = 300
_ACQUIRE_POLL_S = 0.1
_DEFAULT_TIMEOUT_S = 10.0


class CompactionLockError(Exception):
    """Raised when the compaction lock cannot be acquired within the timeout."""


def _resolve_db_path() -> str:
    from context_engine.store import db as mes_memory_store

    return str(mes_memory_store.get_db_path())


def _instance_id() -> str:
    import os
    import socket

    return f"{socket.gethostname()}-{os.getpid()}"


class CompactionLock:
    """Atomic per-session compaction lock stored in the MesMemory database."""

    def __init__(self, db_path: str | None = None, ttl_s: int = _DEFAULT_TTL_S):
        self._db_path_override = db_path
        self._ttl_s = ttl_s

    def _db_path(self) -> str:
        return self._db_path_override or _resolve_db_path()

    def _connect(self) -> sqlite3.Connection:
        # The MesMemory store opens its connection lazily, so its parent
        # directory is not guaranteed to exist yet; SQLite cannot create the
        # file (nor the directory) itself.
        db_path = self._db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(db_path, timeout=5.0)

    @contextmanager
    def acquire_sync(
        self, session_id: str, timeout_s: float = _DEFAULT_TIMEOUT_S
    ) -> Iterator[None]:
        """Blocking acquire; raises CompactionLockError on timeout."""
        holder = self._new_holder()
        deadline = time.monotonic() + timeout_s
        while True:
            if self._try_acquire(session_id, holder):
                logger.debug("compaction lock acquired: session={} holder={}", session_id, holder)
                try:
                    yield
                finally:
                    self._release(session_id, holder)
                return
            if time.monotonic() >= deadline:
                raise CompactionLockError(
                    f"compaction lock not acquired within {timeout_s}s: "
                    f"session={session_id}, holder={self._current_holder(session_id)}"
                )
            time.sleep(_ACQUIRE_POLL_S)

    @asynccontextmanager
    async def acquire(
        self, session_id: str, timeout_s: float = _DEFAULT_TIMEOUT_S
    ) -> AsyncIterator[None]:
        """Async acquire; raises CompactionLockError on timeout."""
        holder = self._new_holder()
        deadline = time.monotonic() + timeout_s
        while True:
            if self._try_acquire(session_id, holder):
                logger.debug("compaction lock acquired: session={} holder={}", session_id, holder)
                try:
                    yield
                finally:
                    self._release(session_id, holder)
                return
            if time.monotonic() >= deadline:
                raise CompactionLockError(
                    f"compaction lock not acquired within {timeout_s}s: "
                    f"session={session_id}, holder={self._current_holder(session_id)}"
                )
            await asyncio.sleep(_ACQUIRE_POLL_S)

    def _new_holder(self) -> str:
        return f"{_instance_id()}-{uuid.uuid4().hex[:8]}"

    def _try_acquire(self, session_id: str, holder: str) -> bool:
        now = time.time()
        conn = self._connect()
        try:
            # Self-healing DDL: the lock may point at a fresh database that has
            # not gone through the MesMemory migrations yet.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS compression_locks ("
                "session_id TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at REAL NOT NULL, "
                "ttl INTEGER NOT NULL DEFAULT 300, renew_count INTEGER DEFAULT 0)"
            )
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ? AND acquired_at + ttl < ?",
                (session_id, now),
            )
            conn.execute(
                "INSERT INTO compression_locks (session_id, holder, acquired_at, ttl) "
                "VALUES (?, ?, ?, ?)",
                (session_id, holder, now, self._ttl_s),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def _release(self, session_id: str, holder: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
                (session_id, holder),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("compaction lock release failed: session={}", session_id)
        finally:
            conn.close()

    def _current_holder(self, session_id: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT holder FROM compression_locks WHERE session_id = ?", (session_id,)
            ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()
