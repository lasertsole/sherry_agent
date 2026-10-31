"""Loop-safe AsyncSqliteSaver fork.

Replaces ``asyncio.Lock`` with a custom ``_LoopSafeLock`` whose
``_get_loop()`` always returns the **running** event loop instead of the
loop that was active at ``__init__`` time.  This avoids the
``RuntimeError: Lock is bound to a different event loop`` error that
occurs when ``AsyncSqliteSaver`` is created on one event loop but used
on another (e.g. under Robyn + nest_asyncio where each HTTP request
may run on a different loop).

All ``async with self.lock`` semantics are preserved — no deadlock risk
(``threading.Lock`` + ``await`` anti-pattern avoided).

Usage
-----
    from agent.checkpointer.thread_safe_checkpointer import (
        ThreadSafeAsyncSqliteSaver,
    )

    conn = await aiosqlite.connect("checkpoints.db", check_same_thread=False)
    checkpointer = ThreadSafeAsyncSqliteSaver(conn)  # no loop binding
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import aiosqlite
import contextlib

from agent.checkpointer.pragmas import connect_with_busy_timeout
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
    SerializerProtocol,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from loguru import logger

# Reclaim the freelist left behind by keep-latest pruning only once the freed
# space is worth a full-file rewrite: ``VACUUM`` copies the whole database, so
# a few MB of free pages are cheaper to leave in place than to reclaim on every
# ``built_agent()`` call.
_VACUUM_THRESHOLD_BYTES = 10 * 1024 * 1024

# ---------------------------------------------------------------------------
# _LoopSafeLock  —  asyncio.Lock that never binds to a specific loop
# ---------------------------------------------------------------------------
# Python's ``asyncio.Lock._get_loop()`` (inherited from ``_LoopBoundMixin``)
# returns ``self._loop`` which was captured at ``__init__`` time via
# ``asyncio.get_running_loop()``.  We override ``_get_loop`` so it always
# returns the **current** running loop, making the lock safe to pass across
# event loops.
#
# This is a minimal, safe override.  The lock's internal state (``_waiters``,
# ``_locked``) is plain Python data — not tied to any loop.
# ---------------------------------------------------------------------------


class _LoopSafeLock(asyncio.Lock):
    """An ``asyncio.Lock`` whose ``_get_loop`` always returns the
    currently running event loop."""

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()


# ---------------------------------------------------------------------------
# ThreadSafeAsyncSqliteSaver
# ---------------------------------------------------------------------------


async def aclean_all_checkpoints() -> dict[str, int]:
    """Keep only the latest checkpoint per thread — ONE global prune.

    Runs at boot (and from a maintenance call), never per turn: this used to be
    called from every graph build, i.e. every turn, which made the shared
    checkpoint DB take a competing writer per turn and turned ordinary
    concurrency into ``SQLITE_BUSY`` turn failures.

    The prune runs inside ``BEGIN IMMEDIATE`` so the keep-set it computes cannot
    go stale: without the write lock another session could commit checkpoint
    N+1 between the SELECT and the DELETE, and ``checkpoint_id != N`` would
    delete that brand-new row.

    Keeps the newest TOP-LEVEL (``checkpoint_ns = ''``) checkpoint per thread —
    the row ``aget_state`` / ``sessions_messages`` reads — mirroring
    :meth:`ThreadSafeAsyncSqliteSaver.aclean_thread_checkpoints`. Writes survive
    only for that ``(checkpoint_ns, checkpoint_id)`` pair; orphan writes are
    dropped. VACUUM is conditional and fail-open.
    """
    from config import SRC_DIR

    db_path = str((SRC_DIR / "checkpoints" / "sqlite.db").resolve())
    if not Path(db_path).exists():
        return dict(threads_kept=0, deleted_checkpoints=0, deleted_writes=0)

    conn = await connect_with_busy_timeout(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id FROM ("
                "  SELECT thread_id, checkpoint_ns, checkpoint_id,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY thread_id"
                "           ORDER BY (checkpoint_ns = '') DESC, rowid DESC"
                "         ) AS rn"
                "  FROM checkpoints"
                ") WHERE rn = 1"
            )
            keep_rows = await cursor.fetchall()
            await cursor.close()

            total_kept = len(keep_rows)
            total_del_ckpt = 0
            total_del_write = 0
            for tid, keep_ns, keep_id in keep_rows:
                cursor = await conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ? "
                    "AND NOT (checkpoint_ns = ? AND checkpoint_id = ?)",
                    (tid, keep_ns, keep_id),
                )
                total_del_ckpt += cursor.rowcount
                await cursor.close()

                cursor = await conn.execute(
                    "DELETE FROM writes WHERE thread_id = ? "
                    "AND NOT (checkpoint_ns = ? AND checkpoint_id = ?)",
                    (tid, keep_ns, keep_id),
                )
                total_del_write += cursor.rowcount
                await cursor.close()

            # orphan writes (their checkpoint is gone entirely)
            cursor = await conn.execute(
                "DELETE FROM writes WHERE NOT EXISTS ("
                "  SELECT 1 FROM checkpoints c"
                "   WHERE c.thread_id = writes.thread_id"
                "     AND c.checkpoint_ns = writes.checkpoint_ns"
                "     AND c.checkpoint_id = writes.checkpoint_id"
                ")"
            )
            total_del_write += cursor.rowcount
            await cursor.close()
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

        try:
            cursor = await conn.execute("PRAGMA freelist_count")
            row = await cursor.fetchone()
            await cursor.close()
            freelist_count = int(row[0]) if row else 0

            cursor = await conn.execute("PRAGMA page_size")
            row = await cursor.fetchone()
            await cursor.close()
            page_size = int(row[0]) if row else 0

            freelist_bytes = freelist_count * page_size
            if freelist_bytes > _VACUUM_THRESHOLD_BYTES:
                await conn.execute("VACUUM")
                logger.info(
                    "checkpoint db vacuumed: reclaimed ~{:.0f}MB",
                    freelist_bytes / (1024 * 1024),
                )
        except Exception as exc:
            logger.warning("checkpoint db vacuum skipped: {}", exc)
    finally:
        await conn.close()

    return dict(
        threads_kept=total_kept,
        deleted_checkpoints=total_del_ckpt,
        deleted_writes=total_del_write,
    )


class ThreadSafeAsyncSqliteSaver(AsyncSqliteSaver):
    """AsyncSqliteSaver variant that uses a loop-safe lock.

    The key difference from the parent:
      - ``self.lock`` is a ``_LoopSafeLock`` (not bound to any specific loop)
      - ``self.loop`` is **not** stored

    All ``async with self.lock`` usage is inherited unchanged from the
    parent — no method overrides are needed.
    """

    lock: asyncio.Lock  # type: ignore[reportIncompatibleVariableOverride]
    loop: Any  # explicitly None, avoids variance check
    _closed: bool

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        # Skip AsyncSqliteSaver.__init__ entirely – it creates an
        # asyncio.Lock (which binds to the current loop) and captures
        # self.loop.  Jump straight to BaseCheckpointSaver.
        super(AsyncSqliteSaver, self).__init__(serde=serde)  # type: ignore[call-overload]
        self.jsonplus_serde = JsonPlusSerializer()
        self.conn = conn
        self.lock = _LoopSafeLock()  # type: ignore[assignment]
        self.loop = None  # explicitly no loop binding
        self.is_setup = False
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle — release the owned aiosqlite connection
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the owned aiosqlite connection (idempotent).

        The factory that builds this saver also opens ``conn``, so the saver
        is its sole owner: closing here releases the connection's non-daemon
        worker thread and its file handle. Repeated calls are no-ops. No sync
        bridge is offered — ``close()`` would have to call ``asyncio.run`` and
        deadlock on a thread that already has a loop.
        """
        if self._closed:
            return
        self._closed = True
        await self.conn.close()

    # ------------------------------------------------------------------
    # Sync bridge methods – adapted for self.loop = None
    # ------------------------------------------------------------------
    # The parent's sync bridges use self.loop to dispatch coroutines via
    # run_coroutine_threadsafe.  Since we intentionally dropped the loop
    # binding, these are only safe to call when there is NO running loop
    # (e.g. from a pure sync thread).  If the caller's thread has a
    # running loop we raise a clear error instead of silently deadlocking.

    @staticmethod
    def _raise_if_has_running_loop(method_name: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop – safe to call sync bridge
        raise RuntimeError(
            f"{method_name}() is not supported on "
            f"ThreadSafeAsyncSqliteSaver from a thread that has a "
            f"running event loop. Use the async variant "
            f"(a{method_name}) instead."
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._raise_if_has_running_loop("get_tuple")
        return asyncio.run(self.aget_tuple(config))  # type: ignore[arg-type]

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        self._raise_if_has_running_loop("list")
        loop = asyncio.new_event_loop()
        aiter_ = self.alist(config, filter=filter, before=before, limit=limit)
        try:
            while True:
                try:
                    yield loop.run_until_complete(anext(aiter_))  # type: ignore[arg-type]
                except StopAsyncIteration:
                    break
        finally:
            # Close the generator BEFORE the loop: its `finally` blocks are
            # coroutines that need a loop to run (an abandoned generator would
            # otherwise be finalized against a closed loop).
            with contextlib.suppress(Exception):
                loop.run_until_complete(aiter_.aclose())
            loop.close()

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._raise_if_has_running_loop("put")
        return asyncio.run(self.aput(config, checkpoint, metadata, new_versions))  # type: ignore[arg-type]

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._raise_if_has_running_loop("put_writes")
        return asyncio.run(  # type: ignore[arg-type]
            self.aput_writes(config, writes, task_id, task_path)
        )

    def delete_thread(self, thread_id: str) -> None:
        self._raise_if_has_running_loop("delete_thread")
        return asyncio.run(self.adelete_thread(thread_id))  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Cleanup — keep only the latest checkpoint per thread
    # ------------------------------------------------------------------

    async def aclean_old_checkpoints(self) -> dict[str, int]:
        """Keep only the latest checkpoint per thread (see the module function)."""
        return await aclean_all_checkpoints()

    async def aclean_thread_checkpoints(self, thread_id: str) -> dict[str, int]:
        """Keep one checkpoint for ``thread_id`` and delete every other row for it.

        The kept row is the newest **top-level** checkpoint (``checkpoint_ns=''``)
        — the one ``aget_state`` / ``sessions_messages`` reads. If the thread has
        no top-level row (legacy damage), the newest row in any namespace is
        kept as a fallback so the thread is never emptied. Writes are kept only
        for the selected ``(checkpoint_ns, checkpoint_id)`` pair.

        A separate connection is used, mirroring ``aclean_old_checkpoints``:
        this never contends with the child's checkpointer connection. No VACUUM
        here — per-child VACUUM would be expensive; the global cleanup keeps the
        10 MB freelist threshold.
        """
        if not str(thread_id).strip():
            return dict(kept=0, deleted_checkpoints=0, deleted_writes=0)

        from pathlib import Path
        from config import SRC_DIR

        db_path = Path((SRC_DIR / "checkpoints" / "sqlite.db").resolve())
        if not db_path.exists():
            return dict(kept=0, deleted_checkpoints=0, deleted_writes=0)

        conn = await connect_with_busy_timeout(str(db_path))
        try:
            cursor = await conn.execute(
                "SELECT checkpoint_ns, checkpoint_id FROM checkpoints "
                "WHERE thread_id = ? "
                "ORDER BY (checkpoint_ns = '') DESC, rowid DESC LIMIT 1",
                (str(thread_id),),
            )
            keep = await cursor.fetchone()
            await cursor.close()

            if keep is None:
                cursor = await conn.execute(
                    "DELETE FROM writes WHERE thread_id = ?", (str(thread_id),)
                )
                deleted_writes = cursor.rowcount
                await cursor.close()
                await conn.commit()
                return dict(kept=0, deleted_checkpoints=0, deleted_writes=deleted_writes)

            keep_ns, keep_id = keep
            cursor = await conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ? "
                "AND NOT (checkpoint_ns = ? AND checkpoint_id = ?)",
                (str(thread_id), keep_ns, keep_id),
            )
            deleted_checkpoints = cursor.rowcount
            await cursor.close()
            cursor = await conn.execute(
                "DELETE FROM writes WHERE thread_id = ? "
                "AND NOT (checkpoint_ns = ? AND checkpoint_id = ?)",
                (str(thread_id), keep_ns, keep_id),
            )
            deleted_writes = cursor.rowcount
            await cursor.close()
            await conn.commit()
            return dict(
                kept=1,
                deleted_checkpoints=deleted_checkpoints,
                deleted_writes=deleted_writes,
            )
        finally:
            await conn.close()

    def get_delta_channel_history(
        self,
        *,
        config: RunnableConfig,
        channels: Sequence[str],
    ) -> Mapping[str, DeltaChannelHistory]:
        self._raise_if_has_running_loop("get_delta_channel_history")
        return asyncio.run(  # type: ignore[arg-type]
            self.aget_delta_channel_history(config=config, channels=channels)
        )
