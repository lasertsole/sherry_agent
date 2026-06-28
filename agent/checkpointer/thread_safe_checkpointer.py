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
from typing import Any

import aiosqlite
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
        try:
            aiter_ = self.alist(config, filter=filter, before=before, limit=limit)
            while True:
                try:
                    yield loop.run_until_complete(anext(aiter_))  # type: ignore[arg-type]
                except StopAsyncIteration:
                    break
        finally:
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
