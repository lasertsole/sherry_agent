"""Process- and session-level resource locks for shell / interaction tools.

The batch scheduler orders tool calls it can classify statically. This layer
covers the two resources a batch plan cannot safely infer from arguments:

* ``system`` — ``terminal`` / ``python_repl`` / ``execute_code`` / ``ptc`` run
  shell or arbitrary code on the same machine; a process-wide lock prevents
  two agents from doing that at once. Off by default: it can serialize a lot of
  legitimate work.
* ``user_interaction`` — ``question`` / ``exit_plan_mode`` share one HITL
  channel per session; a per-session lock prevents two prompts from racing.
  On by default.

The lock is held around the whole handler, so cancellation/exception paths
release it automatically. ``workspace`` / ``session`` / ``network`` are not
locked here: the file tools, memory store, SQLite stores and browser manager
already own their respective mechanisms.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

from config.features import TOOL_PARALLEL

__all__ = ["ToolResourceLockMiddleware"]

_ASYNC_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)
_SYNC_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _session_id(state: Any) -> str:
    if isinstance(state, dict):
        value = state.get("session_id")
        return value if isinstance(value, str) else ""
    value = getattr(state, "session_id", None)
    return value if isinstance(value, str) else ""


def _async_lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _LOCKS_GUARD:
        by_key = _ASYNC_LOCKS.get(loop)
        if by_key is None:
            by_key = {}
            _ASYNC_LOCKS[loop] = by_key
        lock = by_key.get(key)
        if lock is None:
            lock = asyncio.Lock()
            by_key[key] = lock
        return lock


def _sync_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _SYNC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SYNC_LOCKS[key] = lock
        return lock


class ToolResourceLockMiddleware(AgentMiddleware):
    """Lock only the two resources whose exclusivity the batch plan cannot infer."""

    def _lock_key(self, request: ToolCallRequest) -> str | None:
        tool = request.tool
        meta = getattr(tool, "metadata", None)
        if not isinstance(meta, dict):
            return None
        scope = meta.get("side_effect_scope")
        if scope == "system" and TOOL_PARALLEL["system_lock"]:
            return "system"
        if scope == "user_interaction" and TOOL_PARALLEL["interaction_lock"]:
            session_id = _session_id(request.state)
            return f"interaction:{session_id}" if session_id else "interaction"
        return None

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        key = self._lock_key(request)
        if key is None:
            return await handler(request)
        lock = _async_lock(key)
        async with lock:
            return await handler(request)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        key = self._lock_key(request)
        if key is None:
            return handler(request)
        logger.debug("ToolResourceLock: waiting for {}", key)
        with _sync_lock(key):
            return handler(request)
