"""Message persistence at two boundaries: model call and tool return.

Timing contract:

- ``after_model`` / ``aafter_model`` flush every message produced since the
  previous boundary (human / ai / tool): human at the turn's first boundary,
  the AI message right after it is produced, HITL artificial denial
  ToolMessages at the next boundary.
- ``wrap_tool_call`` / ``awrap_tool_call`` flush a tool result the moment the
  handler returns, so it no longer waits for the next model boundary. The
  response is returned untouched. HITL denials short-circuit OUTSIDE this
  layer (HITL wraps this middleware from the outside), so they stay on the
  boundary path by design.

Both paths share one batch pipeline: role/marker filter → watermark →
HITL-denial re-pairing + tool-result dedup → write → mark. The persistent
watermark makes the pair idempotent: graph state accumulates and the same
message is seen again at later boundaries, but each message is stored exactly
once. The watermark lookup checks both the LangGraph message id and the
content fingerprint — a wrap-persisted tool result is written before the graph
reducer assigns it an id, so the later boundary (and a post-restart replay)
matches on the fingerprint.

Ordering contract (``agent/core.py``): registered right after ``HumanInTheLoop``
and before ``Summarization``. Because ``after_model`` nodes chain in reverse
registration order, this is the FIRST after_model hook to run: the AI message
is persisted before HITL strips denied tool calls or raises ``GraphInterrupt``,
and no other hook can skip the flush. In the tool-call wrap chain (first
registered = outermost) this middleware sits innermost, so it sees the real
tool result while HITL short-circuits bypass it.

Failure policy: fail-open. A missing ``session_id`` or an empty candidate
batch skips without writing or raising; a writer failure is logged and NOT
tombstoned, so the same messages are retried at the next boundary.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from loguru import logger

from context_engine import (
    add_messages,
    add_messages_sync,
    filter_persisted_message_ids,
    mark_message_ids_persisted,
)
from agent.middlewares.base import require_session_id

from .prepare import (
    _dedup_tool_results,
    _is_persistable,
    _reconcile_denials_for_persistence,
    _watermark_key,
    _watermark_lookup_keys,
)

__all__ = ["MessagePersistenceMiddleware"]

_SESSION_ID_ERROR = "MessagePersistenceMiddleware: session_id is required"


def _collect_candidates(
    session_id: str, messages: list[BaseMessage]
) -> list[tuple[BaseMessage, list[str]]]:
    """Return ``(message, lookup_keys)`` pairs not yet written for the session.

    ``_is_persistable`` first (role filter + in-process marker) keeps the
    steady-state batch tiny: only messages produced since the last flush
    survive it, so the watermark query never scans the whole history. After a
    restart (markers lost) every message is a candidate and the persistent
    ``persisted_message_ids`` watermark filters the replay instead — on any of
    the message's lookup keys, so a tool result written before its id existed
    is still recognised.
    """
    candidates: list[BaseMessage] = []
    lookup_keys: list[list[str]] = []
    keys_to_check: list[str] = []
    for message in messages:
        if not _is_persistable(message):
            continue
        keys = _watermark_lookup_keys(message)
        candidates.append(message)
        lookup_keys.append(keys)
        keys_to_check.extend(keys)
    if not candidates:
        return []
    already_persisted = filter_persisted_message_ids(session_id, keys_to_check)
    return [
        (message, keys)
        for message, keys in zip(candidates, lookup_keys, strict=True)
        if not any(key in already_persisted for key in keys)
    ]


def _prepare_batch(batch: list[tuple[BaseMessage, list[str]]]) -> list[BaseMessage]:
    """Reconcile HITL denials and drop empty/duplicate tool results."""
    return _dedup_tool_results(
        _reconcile_denials_for_persistence([message for message, _ in batch])
    )


def _iter_tool_messages(response: Any) -> Iterator[ToolMessage]:
    """Yield the ToolMessages carried by a ``wrap_tool_call`` handler response.

    langgraph's tool node can hand the wrapper a bare ``ToolMessage``, a list
    mixing ``ToolMessage`` / ``Command`` items, or a ``Command`` whose
    ``update`` carries messages. Everything else is ignored — only real
    ``ToolMessage`` objects are persisted.
    """
    if isinstance(response, ToolMessage):
        yield response
    elif isinstance(response, Command):
        update = response.update
        if isinstance(update, dict):
            yield from _iter_tool_messages(update.get("messages"))
        elif isinstance(update, list):
            yield from _iter_tool_messages(update)
    elif isinstance(response, (list, tuple)):
        for item in response:
            yield from _iter_tool_messages(item)


def _now_monotonic() -> float:
    """Clock indirection: tests inject a fake here instead of patching ``time``
    (loguru reads the same global clock while logging, which would consume a
    scripted iterator)."""
    return time.monotonic()


def _stamp_tool_duration(response: Any, started: float) -> None:
    """Record the tool's wall-clock duration on every ToolMessage it returned.

    ``time.monotonic()`` deliberately: a wall-clock subtraction goes negative
    when NTP steps the clock back mid-call (ZCode ships that defect — three of
    its call sites subtract ``Date.now()`` unguarded), while a monotonic delta
    cannot. The value rides in ``additional_kwargs`` so the row builder can put
    it in ``messages.tool_duration_ms`` — one authoritative measurement, read
    back by history replay.
    """
    # round, not truncate: 0.85 s is 850 ms, not 849 (float 0.85*1000 is
    # 849.9999…; truncation would systematically under-report).
    elapsed = max(0, round((_now_monotonic() - started) * 1000))
    for message in _iter_tool_messages(response):
        try:
            kwargs = getattr(message, "additional_kwargs", None)
            if isinstance(kwargs, dict):
                kwargs["tool_duration_ms"] = elapsed
        except Exception as exc:  # noqa: BLE001 — timing must never fail a tool
            logger.debug("tool duration stamp skipped: {}", exc)
    # The one log line that makes every tool call observable: the family-level
    # loggers are sparse by design, and turn_id/session_id arrive from the
    # turn's contextualize scope, so this needs no per-tool logging.
    for message in _iter_tool_messages(response):
        logger.info(
            "tool executed: name={} duration_ms={} status={}",
            getattr(message, "name", "") or "unknown",
            elapsed,
            "error" if getattr(message, "status", None) == "error" else "ok",
        )


class MessagePersistenceMiddleware(AgentMiddleware):
    """Flush new messages at every model boundary and every tool return."""

    @staticmethod
    def _resolve_session_id(state: Any) -> str | None:
        """Resolve the session id; a non-dict / blank state skips the flush.

        ``Any`` because ``ToolCallRequest.state`` is not statically typed (the
        tool node documents dict, list and BaseModel forms).
        """
        if not isinstance(state, dict):
            return None
        try:
            return require_session_id(state, _SESSION_ID_ERROR)
        except RuntimeError:
            logger.debug("message persistence: no session_id in state, skipping")
            return None

    def _persist_sync(self, session_id: str, messages: list[BaseMessage], source: str) -> None:
        """Write the batch's new messages; fail-open (never raises)."""
        try:
            batch = _collect_candidates(session_id, messages)
            if not batch:
                return
            prepared = _prepare_batch(batch)
            if not prepared:
                return
            add_messages_sync(session_id, prepared)
            mark_message_ids_persisted(
                session_id, [_watermark_key(message) for message, _ in batch]
            )
            logger.debug(
                "message persistence: wrote {} messages at {} for {}",
                len(prepared),
                source,
                session_id,
            )
        except Exception:
            logger.exception("message persistence failed (fail-open)")

    async def _persist_async(
        self, session_id: str, messages: list[BaseMessage], source: str
    ) -> None:
        """Async twin of :meth:`_persist_sync` (the production path).

        The blocking store I/O around the batch write — the watermark SELECT in
        :func:`_collect_candidates` and the watermark UPDATE — runs on worker
        threads so the event loop never executes SQLite; the batch write itself
        already offloads inside :func:`add_messages`.
        """
        try:
            batch = await asyncio.to_thread(_collect_candidates, session_id, messages)
            if not batch:
                return
            prepared = _prepare_batch(batch)
            if not prepared:
                return
            await add_messages(session_id, prepared)
            await asyncio.to_thread(
                mark_message_ids_persisted,
                session_id,
                [_watermark_key(message) for message, _ in batch],
            )
            logger.debug(
                "message persistence: wrote {} messages at {} for {}",
                len(prepared),
                source,
                session_id,
            )
        except Exception:
            logger.exception("message persistence failed (fail-open)")

    @override
    def after_model(self, state: AgentState, runtime: Runtime | None = None) -> None:
        """Flush messages produced since the previous boundary (sync path)."""
        session_id = self._resolve_session_id(state)
        if session_id is None:
            return
        self._persist_sync(session_id, state.get("messages") or [], "model boundary")

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime | None = None) -> None:
        """Async twin of :meth:`after_model` (the production path)."""
        session_id = self._resolve_session_id(state)
        if session_id is None:
            return
        await self._persist_async(session_id, state.get("messages") or [], "model boundary")

    @override
    @staticmethod
    def _tool_error_message(request: ToolCallRequest, error: Exception) -> ToolMessage:
        """Turn a raising tool into a readable error result.

        langgraph's default tool-error handler only converts its OWN
        ``ToolInvocationError``; anything a tool raises itself is re-raised and
        aborts the whole run (measured live: an empty ``todowrite`` payload killed
        two consecutive turns). One boundary here gives the model the message it
        needs to correct the call — the turn keeps going.
        """
        call = request.tool_call or {}
        name = str(call.get("name") or getattr(request.tool, "name", "tool"))
        return ToolMessage(
            content=f"Error: {error}",
            name=name,
            tool_call_id=str(call.get("id") or ""),
            status="error",
        )

    def _call_sync(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        try:
            return handler(request)
        except GraphInterrupt:
            raise
        except Exception as error:  # noqa: BLE001 - see _tool_error_message
            logger.warning("Tool {} raised: {}", (request.tool_call or {}).get("name"), error)
            return self._tool_error_message(request, error)

    async def _call_async(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        try:
            return await handler(request)
        except GraphInterrupt:
            raise
        except Exception as error:  # noqa: BLE001 - see _tool_error_message
            logger.warning("Tool {} raised: {}", (request.tool_call or {}).get("name"), error)
            return self._tool_error_message(request, error)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Run the tool, flush its ToolMessages, return the response untouched."""
        started = time.monotonic()
        response = self._call_sync(request, handler)
        _stamp_tool_duration(response, started)
        session_id = self._resolve_session_id(request.state)
        if session_id is not None:
            self._persist_sync(session_id, list(_iter_tool_messages(response)), "tool return")
        return response

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Async twin of :meth:`wrap_tool_call`."""
        started = time.monotonic()
        response = await self._call_async(request, handler)
        _stamp_tool_duration(response, started)
        session_id = self._resolve_session_id(request.state)
        if session_id is not None:
            await self._persist_async(
                session_id, list(_iter_tool_messages(response)), "tool return"
            )
        return response
