"""Per-model-boundary persistence of conversation messages into MesMemory.

The persistence timing changed from "compression time" to "every model-call
boundary": each ``after_model`` / ``aafter_model`` invocation flushes every
message produced since the previous boundary (human / ai / tool) into the
``messages`` table, so the raw store no longer depends on a compaction ever
firing. ``persisted_message_ids`` keeps the flush write-once: graph state
accumulates, the same batch is seen again at every later boundary, and the
watermark filters it — including after a process restart, where the in-process
``_db_persisted`` markers are gone but the message ids are stable.

Ordering contract (``agent/core.py``): the middleware is registered right after
``HumanInTheLoop``, which makes its ``after_model`` node the FIRST one to run
after ``model`` — LangChain 1.3.9 chains ``after_model`` nodes in reverse
registration order (``langchain/agents/factory.py``: ``model`` →
``after_model[-1]`` → … → ``after_model[0]``). Consequences:

- the AI message is persisted before HITL strips denied tool calls or raises
  ``GraphInterrupt`` — an interrupt can no longer skip the flush;
- no other hook exception can prevent the flush (fail-open on our side only);
- the flush runs before ``Summarization`` can replace messages in a later
  ``wrap_model_call`` (``Summarization`` has no ``after_model`` hook).

Latency semantics: tool results (and HITL artificial denial ToolMessages) are
visible at the NEXT model boundary; human messages are flushed at the current
turn's first ``after_model``; the AI message right after it is produced.

Failure policy: fail-open. A missing ``session_id`` (child/nudge graphs) or an
empty candidate batch skips without writing or raising; a writer failure is
logged and NOT tombstoned, so the same messages are retried at the next
boundary.
"""

from typing import override

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage
from langgraph.runtime import Runtime
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
)

__all__ = ["MessagePersistenceMiddleware"]

_SESSION_ID_ERROR = "MessagePersistenceMiddleware: session_id is required"


def _collect_candidates(
    session_id: str, messages: list[BaseMessage]
) -> list[tuple[BaseMessage, str]]:
    """Return ``(message, watermark_key)`` pairs not yet written for the session.

    ``_is_persistable`` first (role filter + in-process marker) keeps the
    steady-state batch tiny: only messages produced since the last boundary
    survive it, so the watermark query never scans the whole history. After a
    restart (markers lost) every message is a candidate and the persistent
    ``persisted_message_ids`` watermark filters the replay instead.
    """
    candidates: list[BaseMessage] = []
    keys: list[str] = []
    for message in messages:
        if not _is_persistable(message):
            continue
        candidates.append(message)
        keys.append(_watermark_key(message))
    if not candidates:
        return []
    already_persisted = filter_persisted_message_ids(session_id, keys)
    return [
        (message, key)
        for message, key in zip(candidates, keys, strict=True)
        if key not in already_persisted
    ]


def _prepare_batch(batch: list[tuple[BaseMessage, str]]) -> list[BaseMessage]:
    """Reconcile HITL denials and drop empty/duplicate tool results."""
    return _dedup_tool_results(
        _reconcile_denials_for_persistence([message for message, _ in batch])
    )


class MessagePersistenceMiddleware(AgentMiddleware):
    """Flush every new graph-state message at each model-call boundary."""

    @staticmethod
    def _resolve_session_id(state: AgentState) -> str | None:
        try:
            return require_session_id(state, _SESSION_ID_ERROR)
        except RuntimeError:
            logger.debug("message persistence: no session_id in state, skipping")
            return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime | None = None) -> None:
        """Sync twin of :meth:`aafter_model` (non-production path).

        Requires no event loop: ``add_messages_sync`` runs on the current
        thread. A blank/missing ``session_id`` or an empty candidate batch
        skips without writing or raising.
        """
        session_id = self._resolve_session_id(state)
        if session_id is None:
            return
        try:
            batch = _collect_candidates(session_id, state.get("messages") or [])
            if not batch:
                return
            prepared = _prepare_batch(batch)
            if not prepared:
                return
            add_messages_sync(session_id, prepared)
            mark_message_ids_persisted(session_id, [key for _, key in batch])
            logger.debug(
                "message persistence: wrote {} messages at model boundary for {}",
                len(prepared),
                session_id,
            )
        except Exception:
            logger.exception("message persistence failed (fail-open)")

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime | None = None) -> None:
        """Async twin of :meth:`after_model` (the production path)."""
        session_id = self._resolve_session_id(state)
        if session_id is None:
            return
        try:
            batch = _collect_candidates(session_id, state.get("messages") or [])
            if not batch:
                return
            prepared = _prepare_batch(batch)
            if not prepared:
                return
            await add_messages(session_id, prepared)
            mark_message_ids_persisted(session_id, [key for _, key in batch])
            logger.debug(
                "message persistence: wrote {} messages at model boundary for {}",
                len(prepared),
                session_id,
            )
        except Exception:
            logger.exception("message persistence failed (fail-open)")
