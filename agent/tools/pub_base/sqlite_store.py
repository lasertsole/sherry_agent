"""Shared SQLite connection + lazy schema-init skeleton for tool registries.

The todolist, taskflow and subagent registries each own a separate SQLite
database, but all three duplicated the same connection lifecycle:

* every connection sets ``PRAGMA busy_timeout`` as its FIRST statement, so a
  contended write waits for the lock instead of raising
  ``sqlite3.OperationalError: database is locked``;
* the journal-mode switch and the ``CREATE TABLE IF NOT EXISTS`` DDL run ONCE
  per process, lazily, loop-gated under an ``asyncio.Lock`` on the async path
  and a ``threading.Lock`` on the stdlib ``sqlite3`` path;
* a non-owning event loop polls the owner's one-time init instead of queueing
  on a lock that belongs to another loop.

This base class owns that skeleton. Each store keeps its own schema by
overriding the ``_*_ddls`` hooks — no table, column or index definition moves
here, so every DDL string stays byte-identical to what it was.

Per-store process state and configuration (the database path/dir, the busy
timeout and the once-per-process init flags) deliberately stay in the owning
store module. The base reads and writes them through the module namespace
handed in at construction (the store module's ``globals()``), preserving the
module-level contract that tests and other modules use to isolate each store.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger


class BaseSQLiteRepository(ABC):
    """Connection + lazy once-per-process schema init for one SQLite store.

    Subclasses supply the DDL through the ``_*_ddls`` hooks and set
    ``wal_label`` for the WAL-contention warning. The process-level keys the
    base reads from the namespace are ``_DB_DIR``, ``_DB_PATH``,
    ``_BUSY_TIMEOUT_MS``, ``_BUSY_TIMEOUT_S``, ``_INIT_WAIT_TIMEOUT_S``,
    ``_initialized``, ``_init_loop``, ``_init_lock``, ``_sync_tables_ready``
    and ``_sync_init_lock``.
    """

    #: Human-readable store name used in the WAL-contention warning.
    wal_label: str = "sqlite store"

    def __init__(self, namespace: MutableMapping[str, Any]) -> None:
        self._ns = namespace

    # -- schema hooks ------------------------------------------------------

    @abstractmethod
    def _table_ddls(self) -> tuple[str, ...]:
        """Return the DDL statements the async one-time init runs (one per table)."""

    def _sync_table_ddls(self) -> tuple[str, ...]:
        """DDL statements the sync one-time init runs; defaults to the async set."""
        return self._table_ddls()

    def _migration_ddls(self) -> tuple[str, ...]:
        """Additive migration DDL; a duplicate-column error is the expected no-op."""
        return ()

    def _index_ddls(self) -> tuple[str, ...]:
        """Idempotent ``CREATE INDEX IF NOT EXISTS`` statements."""
        return ()

    # -- connection lifecycle ----------------------------------------------

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[aiosqlite.Connection]:
        """Open a short-lived connection; busy_timeout is always the FIRST statement."""
        ns = self._ns
        db = await aiosqlite.connect(ns["_DB_PATH"])
        try:
            await db.execute(f"PRAGMA busy_timeout = {ns['_BUSY_TIMEOUT_MS']}")
            yield db
        finally:
            await db.close()

    async def switch_to_wal_if_needed(self, db: aiosqlite.Connection) -> None:
        """Switch the database to WAL mode, unless it is already WAL.

        The journal-mode switch does NOT reliably honor busy_timeout: with an
        active writer it can raise OperationalError("database is locked")
        immediately. Once the file is WAL (the steady state after the first
        init) the pragma is skipped entirely. If a concurrent initializer is
        mid-switch, re-check and tolerate the outcome; init must never fail
        over the journal mode.
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
                    "{} stays in {!r} journal mode (WAL switch contended); proceeding without WAL",
                    self.wal_label,
                    mode,
                )

    # -- one-time schema init (async) --------------------------------------

    async def _init_db(self) -> None:
        """One-time schema setup; safe to run concurrently (busy_timeout + IF NOT EXISTS)."""
        ns = self._ns
        db_dir: Path = ns["_DB_DIR"]
        db_dir.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await self.switch_to_wal_if_needed(db)
            for ddl in self._table_ddls():
                await db.execute(ddl)
            for ddl in self._migration_ddls():
                try:
                    await db.execute(ddl)
                except aiosqlite.OperationalError:
                    # Column already exists - nothing to do.
                    pass
            for ddl in self._index_ddls():
                await db.execute(ddl)
            await db.commit()

    async def ensure_db(self) -> None:
        """Ensure the database directory and required tables exist (once per process).

        Fast-returns once initialized. On first use the calling loop takes
        ownership and runs the schema init under its ``asyncio.Lock``. Callers
        from other event loops never touch that lock (a foreign-thread release
        would wake a queued waiter via a non-threadsafe call_soon that can
        leave their loop asleep forever); they poll for the owner's one-time
        init and only self-init as a last resort, avoiding a rollback-journal
        stampede of concurrent DDL.
        """
        ns = self._ns
        if ns["_initialized"]:
            return
        loop = asyncio.get_running_loop()
        if ns["_init_loop"] is None:
            ns["_init_loop"] = loop
        if ns["_init_loop"] is loop:
            async with ns["_init_lock"]:
                if ns["_initialized"]:
                    return
                await self._init_db()
                ns["_initialized"] = True
            return
        # Non-owning loop: wait for the owning loop to finish its one-time init.
        deadline = time.monotonic() + ns["_INIT_WAIT_TIMEOUT_S"]
        while not ns["_initialized"] and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not ns["_initialized"]:
            # Owning loop never finished (died mid-init): initialize ourselves.
            await self._init_db()
            ns["_initialized"] = True

    # -- one-time schema init (sync) ---------------------------------------

    def ensure_tables_sync(self) -> None:
        """One-time table creation for the sync (stdlib sqlite3) paths.

        Thread-locked so concurrent sync callers cannot race on DDL; keeps the
        logged-and-swallowed contract of the sync read path.
        """
        ns = self._ns
        if ns["_sync_tables_ready"]:
            return
        with ns["_sync_init_lock"]:
            if ns["_sync_tables_ready"]:
                return
            db_dir: Path = ns["_DB_DIR"]
            db_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(ns["_DB_PATH"]), timeout=ns["_BUSY_TIMEOUT_S"])
            try:
                for ddl in self._sync_table_ddls():
                    conn.execute(ddl)
                for ddl in self._migration_ddls():
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        # Column already exists - nothing to do.
                        pass
                for ddl in self._index_ddls():
                    conn.execute(ddl)
                conn.commit()
            finally:
                conn.close()
            ns["_sync_tables_ready"] = True
