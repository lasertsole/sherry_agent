"""Context eviction middleware (P0-2 / P1-9 / P2-4).

Tool results (P0-2/P2-4): at every tool return the response is intercepted
before it reaches graph state:

- generic tools: a result over ``evict_threshold_chars`` is written under
  ``SESSIONS_DIR/{session_id}/evicted/`` and replaced by a head/tail preview
  carrying the file path;
- ``read_file`` (P2-4): the file is already on disk, so the result is sliced
  to a fixed head plus a recovery notice — no eviction file is written;
- every other tool in ``excluded_tools`` passes through untouched.

Human messages (P1-9): an oversized plain-text ``HumanMessage`` at the end of
the transcript is offloaded at ``before_model`` (full text written under the
same ``evicted/`` directory, ``additional_kwargs["lc_evicted_to"]`` added —
**content and id unchanged**, so the graph reducer updates the message in
place) and its model view is replaced by a head/tail preview at
``wrap_model_call``. This is deliberately the opposite three-state split from
the tool path: state keeps the **full** text (so MesMemory archives the full
text and the compression pipeline still sees it), and only the model view is
truncated. The eviction file is self-healed from the state text whenever it is
missing.

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
from pathlib import Path
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ExtendedModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from loguru import logger

from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION
from context_engine import is_message_persisted, mark_message_ids_persisted
from pub.func.message.eviction import (
    EVICTED_TO_KEY,
    _build_evicted_content,
    _extract_text,
    build_human_preview,
    evict_human_message,
    evict_tool_result,
    get_eviction_dir,
    load_evicted,
    slice_read_file_result,
)
from agent.middlewares.base import require_session_id
from agent.middlewares.message_persistence.prepare import _watermark_key

__all__ = ["ContextEvictionMiddleware"]

_READ_FILE_TOOL = "read_file"
_SESSION_ID_ERROR = "ContextEvictionMiddleware: session_id is required"


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


def _heal_eviction_file(eviction_path: str, text: str, session_id: str | None) -> None:
    """Rewrite a missing eviction file from the state text, in-session only.

    The state keeps the full human text, so a lost file (session dir pruned,
    disk issue) is recoverable at the next model call. The target must be the
    session's own ``evicted/`` directory — a foreign path in the tag is never
    written to.
    """
    if session_id is None:
        return
    eviction_dir = get_eviction_dir(session_id)
    if eviction_dir is None:
        return
    target = Path(eviction_path)
    if target.parent != eviction_dir:
        logger.warning("human eviction: refusing to self-heal outside {}", eviction_dir)
        return
    try:
        target.write_text(text, encoding="utf-8")
        logger.info("human eviction: self-healed {}", target)
    except OSError:
        logger.exception("human eviction: self-heal failed (fail-open)")


def _build_model_view(message: HumanMessage, session_id: str | None) -> HumanMessage:
    """Build the preview-only copy of an evicted human message for the model."""
    text = _extract_text(message.content)
    if not text:
        return message
    eviction_path = message.additional_kwargs.get(EVICTED_TO_KEY)
    if not isinstance(eviction_path, str) or not eviction_path:
        return message
    if load_evicted(eviction_path) is None:
        _heal_eviction_file(eviction_path, text, session_id)
    preview = build_human_preview(text, Path(eviction_path))
    return message.model_copy(update={"content": _build_evicted_content(message.content, preview)})


class ContextEvictionMiddleware(AgentMiddleware):
    """Offload oversized tool results (P0-2/P2-4) and human messages (P1-9)."""

    def __init__(self) -> None:
        self._enabled = TOOL_RESULT_EVICTION["enabled"]
        self._human_enabled = TOOL_RESULT_EVICTION["human_evict_enabled"]

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

    # ── Human messages (P1-9) ─────────────────────────────────────────────

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """Tag the trailing oversized ``HumanMessage`` and offload its text."""
        return self._tag_last_human(state)

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """Async twin of :meth:`before_model` (same sync file work)."""
        return self._tag_last_human(state)

    def _tag_last_human(self, state: Any) -> dict[str, Any] | None:
        """Return the in-place state update for the tagged message, or ``None``.

        Only the LAST message is considered (a past user turn is never
        re-examined), matching the gate in the P1-9 plan. The update carries a
        ``model_copy`` with the same id and content, so the ``add_messages``
        reducer replaces the message in place — the full text stays in state.
        """
        if not self._human_enabled or not isinstance(state, dict):
            return None
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        last = messages[-1]
        if not isinstance(last, HumanMessage):
            return None
        session_id = self._resolve_session_id(state)
        if session_id is None:
            return None
        try:
            tagged = evict_human_message(last, session_id)
        except Exception:
            logger.exception("human message eviction failed (fail-open)")
            return None
        if tagged is None:
            return None
        return {"messages": [tagged]}

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        """Truncate the model view of every evicted human message."""
        if not self._human_enabled:
            return handler(request)
        return handler(self._override_human_views(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage | ExtendedModelResponse:
        """Async twin of :meth:`wrap_model_call`."""
        if not self._human_enabled:
            return await handler(request)
        return await handler(self._override_human_views(request))

    def _override_human_views(self, request: ModelRequest) -> ModelRequest:
        """Apply the preview replacement to the request's message list."""
        session_id = self._resolve_session_id(request.state)
        changed = False
        processed: list[AnyMessage] = []
        for message in request.messages:
            view: AnyMessage = message
            if isinstance(message, HumanMessage) and message.additional_kwargs.get(EVICTED_TO_KEY):
                try:
                    view = _build_model_view(message, session_id)
                except Exception:
                    logger.exception("human message view truncation failed (fail-open)")
            changed = changed or view is not message
            processed.append(view)
        if not changed:
            return request
        return request.override(messages=processed)

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
            logger.debug("context eviction: no session_id in state, skipping")
            return None
