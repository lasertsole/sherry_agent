"""Tool-result eviction middleware (P0-2) + execution-time read_file slice (P2-4).

At every tool return the response is intercepted before it reaches graph
state:

- generic tools: a result over ``evict_threshold_chars`` is written under
  ``SESSIONS_DIR/{session_id}/evicted/`` and replaced by a head/tail preview
  carrying the file path;
- ``read_file`` (P2-4): the file is already on disk, so the result is sliced
  to a fixed head plus a recovery notice — no eviction file is written;
- every other tool in ``excluded_tools`` passes through untouched.

Chain position (``agent/core.py``): registered immediately after
``ToolGuardrails``, which makes it OUTER relative to ``PathGuard`` /
``HumanInTheLoop`` / ``MessagePersistenceMiddleware`` (wrap chain: first
registered = outermost, last = innermost). ``MessagePersistenceMiddleware``
stays innermost, so it flushes the RAW result the moment the handler returns;
this middleware then swaps in the preview on the way out. State and
checkpointer only ever hold the preview, while MesMemory keeps the full text
and the eviction file keeps a byte-identical copy.

Watermark safety: the replacement is built with ``ToolMessage.model_copy``, so
the LangGraph message id and ``additional_kwargs`` (including the in-process
``_db_persisted`` marker set by the inner persistence flush) survive. When the
raw result was already persisted, the replacement's watermark key is also
tombstoned, which covers a restart where the in-process marker is lost and the
replacement's content fingerprint no longer matches the raw one — the next
model boundary never writes a second row for the same logical message.

Session deletion: eviction files live under ``SESSIONS_DIR/{session_id}/`` and
are removed by ``clear_session``'s folder rmtree.

Subagent sessions do not register this middleware (same as message
persistence): child transcripts keep their full tool results.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger

from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION
from context_engine import is_message_persisted, mark_message_ids_persisted
from pub.func.message.eviction import evict_tool_result, slice_read_file_result
from agent.middlewares.base import require_session_id
from agent.middlewares.message_persistence.prepare import _watermark_key

__all__ = ["ToolResultEvictionMiddleware"]

_READ_FILE_TOOL = "read_file"
_SESSION_ID_ERROR = "ToolResultEvictionMiddleware: session_id is required"


def _rewrite_tool_messages(value: Any, rewrite: Callable[[ToolMessage], ToolMessage]) -> Any:
    """Pass every ``ToolMessage`` in a wrap response through *rewrite*.

    Mirrors ``MessagePersistenceMiddleware._iter_tool_messages``: the tool node
    can hand the wrapper a bare ``ToolMessage``, a list mixing ``ToolMessage``
    / ``Command`` items, or a ``Command`` whose ``update`` carries messages.
    Non-ToolMessage items pass through and the original container is returned
    when nothing changed.
    """
    if isinstance(value, ToolMessage):
        return rewrite(value)
    if isinstance(value, (list, tuple)):
        rewritten = [_rewrite_tool_messages(item, rewrite) for item in value]
        if all(new is old for new, old in zip(rewritten, value, strict=True)):
            return value
        return type(value)(rewritten)
    if isinstance(value, Command):
        update = value.update
        if isinstance(update, dict) and update.get("messages") is not None:
            rewritten = _rewrite_tool_messages(update["messages"], rewrite)
            if rewritten is not update["messages"]:
                return replace(value, update={**update, "messages": rewritten})
        elif isinstance(update, list):
            rewritten = _rewrite_tool_messages(update, rewrite)
            if rewritten is not update:
                return replace(value, update=rewritten)
    return value


class ToolResultEvictionMiddleware(AgentMiddleware):
    """Offload oversized tool results; slice ``read_file`` results (P2-4)."""

    def __init__(self) -> None:
        self._enabled = TOOL_RESULT_EVICTION["enabled"]

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Run the tool, replace its result with the evicted/sliced view."""
        return self._apply(request, handler(request))

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Async twin of :meth:`wrap_tool_call`."""
        return self._apply(request, await handler(request))

    def _apply(self, request: ToolCallRequest, response: Any) -> Any:
        if not self._enabled:
            return response
        session_id = self._resolve_session_id(request.state)
        if session_id is None:
            return response
        return _rewrite_tool_messages(
            response, lambda message: self._maybe_evict(message, session_id)
        )

    def _maybe_evict(self, result: ToolMessage, session_id: str) -> ToolMessage:
        """Return the replacement message, or *result* when eviction is skipped."""
        try:
            if (getattr(result, "name", "") or "") == _READ_FILE_TOOL:
                sliced = slice_read_file_result(result)
                return self._cover_with_watermark(session_id, result, sliced)
            evicted = evict_tool_result(result, session_id)
            if evicted is None:
                return result
            return self._cover_with_watermark(session_id, result, evicted)
        except Exception:
            logger.exception("tool result eviction failed (fail-open)")
            return result

    @staticmethod
    def _cover_with_watermark(
        session_id: str, original: ToolMessage, replacement: ToolMessage
    ) -> ToolMessage:
        """Tombstone the replacement's watermark key when the raw result is stored.

        The inner persistence flush marks the raw message in-process;
        ``model_copy`` carries that marker into the replacement. The tombstone
        additionally covers a restart (marker lost, replacement fingerprint
        changed) so the boundary never writes a second row for this message.
        When the raw write failed there is no marker and no tombstone: the
        boundary retries with the replacement content instead.
        """
        if replacement is original or not is_message_persisted(original):
            return replacement
        try:
            mark_message_ids_persisted(session_id, [_watermark_key(replacement)])
        except Exception:
            logger.exception("tool result eviction: watermark update failed (fail-open)")
        return replacement

    @staticmethod
    def _resolve_session_id(state: Any) -> str | None:
        """Resolve the session id; a non-dict / blank state skips eviction."""
        if not isinstance(state, dict):
            return None
        try:
            return require_session_id(state, _SESSION_ID_ERROR)
        except RuntimeError:
            logger.debug("tool result eviction: no session_id in state, skipping")
            return None
