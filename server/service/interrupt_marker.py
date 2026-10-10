"""Interrupted-turn marker writer.

When a streaming turn dies mid-flight (user cancel via ``asyncio.Task.cancel()``
or the heartbeat idle timeout), the checkpointer transcript can end in a
provider-invalid shape (e.g. an ``AIMessage(tool_calls)`` whose results never
arrived) and the client/UI loses the "the answer was cut off" fact. This module
reconciles the graph state and persists the interruption:

1. **Checkpointer reconciliation** — ``agent.state_port.heal_interrupted_turn``
   runs the ONE ``graph.aupdate_state`` commit that (a) heals the TRAILING
   incomplete model super-step and (b) appends the interrupt marker
   ``AIMessage``. The marker carries a DETERMINISTIC message id
   (``interrupted-{thread_id}-{turn_seq}``), so the ``add_messages``
   reducer upserts it — a retried write is an idempotent rewrite, never a
   duplicate (FACT A + FACT D). The checkpoint shape is the agent framework's
   business, so this module only decides WHAT to persist and then persists the
   MesMemory row + voids the queue row.

2. **MesMemory dual-write** — one ``role=ai`` row prefixed
   ``[interrupted:{reason}] `` through the EXISTING store writer
   (``context_engine.store.core.add_messages``; no schema change). MesMemory
   is APPEND-ONLY with no id dedupe (FACT D), so this
   module dedupes itself by scanning the session's latest turn rows for an
   already-present interrupted row.

3. **CLAIMED queue cleanup** — the cancelled turn's ``insert_claimed``
   placeholder row () is flipped to ``VOIDED`` so it is never drained
   as if the turn had run; QUEUED rows are untouched (they keep waiting for
   drain).

Everything is BEST-EFFORT by contract: any internal failure is logged via
loguru and swallowed — this runs ON the cancellation exception paths of
``server.service.messages.async_generate`` and must never mask the cancel
frames or raise into the generator teardown.

Design authority: the interrupt-marker FACT list in the spike tests
(``tests/context_engine/store/test_interrupt_marker_approach.py``; heal at WRITE
time, deterministic-id upsert, marker must not rely on ToolCallNormalize — see
the inline comment at the heal decision).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agent.state_port import heal_interrupted_turn

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import light
    from langgraph.graph.state import CompiledStateGraph

    from server.queue.user_input_queue import UserInputQueue

__all__ = ["write_interrupted_marker"]

InterruptReason = Literal["cancelled", "heartbeat_timeout"]

# MesMemory row prefix carrying the interrupted flag (no schema change; the
# reason rides inside the content text).
_MESMEMORY_PREFIX_TEMPLATE = "[interrupted:{reason}]"


async def write_interrupted_marker(
    session_id: str,
    config: RunnableConfig,
    partial_text: str,
    reason: InterruptReason,
    graph: CompiledStateGraph[Any, Any, Any, Any] | None = None,
    queue: UserInputQueue | None = None,
) -> None:
    """Persist the interrupted-turn marker for ``session_id`` (best-effort).

    Args:
        session_id: Bare session id (queue key / MesMemory key).
        config: The SAME runnable config the agent turn ran with
            (``build_agent_config(session_id)``) — carries
            ``configurable.thread_id`` for the checkpointer.
        partial_text: The answer text produced before the interruption
            (``ai_text`` in ``async_generate``). Empty is legal — the marker
            is still written (content ``"[interrupted]"``).
        reason: ``"cancelled"`` (user stop) or ``"heartbeat_timeout"``.
        graph: Compiled agent graph for state ops. ``None`` (production)
            resolves lazily via ``agent.built_agent()``; tests inject the
            hermetic graph.
        queue: ``UserInputQueue`` for the CLAIMED-row cleanup. ``None``
            (production) resolves the process-wide default ().

    Never raises (except a re-delivered ``CancelledError``): internal
    failures are logged and swallowed — the caller is an exception handler.
    """
    try:
        await _write_interrupted_marker_inner(
            session_id, config, partial_text, reason, graph, queue
        )
    except asyncio.CancelledError:
        # A re-delivered cancellation must keep propagating (never absorbed).
        raise
    except Exception as e:  # noqa: BLE001 - best-effort hook on a cancellation path
        logger.warning(
            "interrupt_marker: write failed (best-effort, no raise): "
            "session_id={!r}, reason={!r}, error={!r}",
            session_id,
            reason,
            e,
        )


async def _write_interrupted_marker_inner(
    session_id: str,
    config: RunnableConfig,
    partial_text: str,
    reason: InterruptReason,
    graph: CompiledStateGraph[Any, Any, Any, Any] | None,
    queue: UserInputQueue | None,
) -> None:
    """Actual reconciliation work (see ``write_interrupted_marker``)."""
    # Lazy import keeps this module cheap to import (it sits on the hot
    # cancellation path).
    if queue is None:
        from server.service.input_queue_service import get_default_queue

        queue = get_default_queue()

    write = await heal_interrupted_turn(session_id, config, partial_text, reason, graph)
    if write.wrote_checkpointer:
        await _persist_to_mesmemory(session_id, reason, partial_text, write.marker_id)

    await _void_claimed_rows(queue, session_id)


async def _persist_to_mesmemory(
    session_id: str, reason: InterruptReason, partial_text: str, marker_id: str
) -> None:
    """Mirror the marker as one ``role=ai`` MesMemory row (existing write path).

    Content prefix ``[interrupted:{reason}] `` carries the flag WITHOUT a
    schema change (metadata does not survive
    into MesMemory rows). ``add_messages`` is APPEND-ONLY with NO id dedupe
    (FACT D), so dedupe happens HERE: the session's
    latest turn rows are scanned for an existing interrupted ai row first.
    """
    from context_engine.store import core as store_core

    prefix = _MESMEMORY_PREFIX_TEMPLATE.format(reason=reason)
    try:
        # Offload the dedupe scan: it is blocking SQLite on the event loop
        # thread (the write below already offloads inside ``add_messages``).
        rows = await asyncio.to_thread(
            store_core.get_messages_by_lastest_n_turns, session_id, last_n=2
        )
    except Exception as e:  # noqa: BLE001 - read failure must not abort the dual-write
        logger.warning(
            "interrupt_marker: MesMemory dedupe scan failed (writing anyway): "
            "session_id={!r}, error={!r}",
            session_id,
            e,
        )
        rows = []

    for row in rows:
        if (
            row.get("role") == "ai"
            and isinstance(row.get("content"), str)
            and row["content"].startswith(prefix)
        ):
            logger.info(
                "interrupt_marker: MesMemory already holds an interrupted "
                "row for session {!r} (reason {!r}); skipping insert",
                session_id,
                reason,
            )
            return

    row_content = f"{prefix} {partial_text}".rstrip()
    await store_core.add_messages(
        session_id,
        [
            AIMessage(
                content=row_content,
                id=marker_id,
                metadata={"interrupted": True, "reason": reason},
            )
        ],
    )
    logger.info(
        "interrupt_marker: MesMemory interrupted row written for session {!r} (reason={!r})",
        session_id,
        reason,
    )


async def _void_claimed_rows(queue: UserInputQueue, session_id: str) -> int:
    """Flip the session's CLAIMED placeholder rows to VOIDED (never QUEUED).

    The interrupted turn will never deliver its result, so its CLAIMED row
    (durable "turn in progress" fact) must not survive as busy —
    that would block fresh turns for 24h (issues.md, P2). QUEUED rows
    stay queued: drain delivers them after the next turn.
    """
    from server.queue.user_input_queue import UserInputQueueStatus

    voided = 0
    for row in await queue.list_active(session_id):
        if row.status is UserInputQueueStatus.CLAIMED:
            await queue.mark_terminal(row.id, "VOIDED")
            voided += 1
    if voided:
        logger.info(
            "interrupt_marker: voided {} CLAIMED queue row(s) for session {!r}",
            voided,
            session_id,
        )
    return voided
