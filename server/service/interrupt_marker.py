"""Interrupted-turn marker writer (plan Task 6, input-queueing-reply-binding).

When a streaming turn dies mid-flight (user cancel via ``asyncio.Task.cancel()``
or the heartbeat idle timeout), the checkpointer transcript can end in a
provider-invalid shape (e.g. an ``AIMessage(tool_calls)`` whose results never
arrived) and the client/UI loses the "the answer was cut off" fact. This module
reconciles the graph state and persists the interruption:

1. **Checkpointer reconciliation** — one ``graph.aupdate_state`` commit that
   (a) heals the TRAILING incomplete model super-step and (b) appends the
   interrupt marker ``AIMessage``. The marker carries a DETERMINISTIC message
   id (``interrupted-{thread_id}-{turn_seq}``), so the ``add_messages``
   reducer upserts it — a retried write is an idempotent rewrite, never a
   duplicate (Task 3 spike verdict, FACT A + FACT D).

2. **MesMemory dual-write** — one ``role=ai`` row prefixed
   ``[interrupted:{reason}] `` through the EXISTING store writer
   (``context_engine.store.core.add_messages``; no schema change). MesMemory
   is APPEND-ONLY with no id dedupe (verdict FACT D clarification), so this
   module dedupes itself by scanning the session's latest turn rows for an
   already-present interrupted row.

3. **CLAIMED queue cleanup** — the cancelled turn's ``insert_claimed``
   placeholder row (Task 5) is flipped to ``VOIDED`` so it is never drained
   as if the turn had run; QUEUED rows are untouched (they keep waiting for
   Task 7's drain).

Everything is BEST-EFFORT by contract: any internal failure is logged via
loguru and swallowed — this runs ON the cancellation exception paths of
``server.service.messages.async_generate`` and must never mask the cancel
frames or raise into the generator teardown.

Design authority: ``.omo/evidence/task-3-spike-verdict.md`` (verdict: heal at
WRITE time, deterministic-id upsert, marker must not rely on ToolCallNormalize
— see the inline comment at the heal decision).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, cast

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import light
    from langgraph.graph.state import CompiledStateGraph

    from server.queue.user_input_queue import UserInputQueue

__all__ = ["write_interrupted_marker"]

InterruptReason = Literal["cancelled", "heartbeat_timeout"]

# MesMemory row prefix carrying the interrupted flag (no schema change; the
# reason rides inside the content text — plan Task 6 approved fallback).
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
            (production) resolves the process-wide default (Task 5).

    Never raises (except a re-delivered ``CancelledError``): internal
    failures are logged and swallowed — the caller is an exception handler.
    """
    try:
        await _write_interrupted_marker_inner(session_id, config, partial_text, reason, graph, queue)
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
    # Lazy imports keep this module cheap to import (it sits on the hot
    # cancellation path) and avoid hard dependency edges in tests.
    if graph is None:
        # agent/__init__ exposes built_agent through module __getattr__ (lazy),
        # which static analysis cannot type — import the real submodule and
        # cast the returned wrapper/graph to the state-ops contract.
        from agent import core as agent_core

        graph = cast("CompiledStateGraph[Any, Any, Any, Any]", await agent_core.built_agent())
    if queue is None:
        from server.service.input_queue_service import get_default_queue

        queue = get_default_queue()

    snapshot = await graph.aget_state(config=config)
    messages: list[BaseMessage] = list((snapshot.values or {}).get("messages", []))

    thread_id = (config.get("configurable") or {}).get("thread_id", session_id)
    # turn_seq = number of HumanMessages in the CURRENT state: stable across
    # marker-write retries (no new human turn can land in between — the turn
    # being interrupted is not finished), deterministic per thread.
    turn_seq = sum(1 for m in messages if m.type == "human")
    marker_id = f"interrupted-{thread_id}-{turn_seq}"

    already_written = any(getattr(m, "id", None) == marker_id for m in messages)
    if already_written:
        # Idempotent rewrite: the deterministic id is already in state — skip
        # the checkpointer write AND the MesMemory insert (MesMemory is
        # append-only with no id dedupe; verdict FACT D), but ALWAYS run the
        # CLAIMED cleanup below.
        logger.info(
            "interrupt_marker: marker {!r} already in state; skipping "
            "checkpointer + MesMemory writes (idempotent rewrite)",
            marker_id,
        )
    else:
        marker = AIMessage(
            content=f"[interrupted] {partial_text}".strip(),
            id=marker_id,
            metadata={"interrupted": True, "reason": reason},
        )
        placeholders = _heal_trailing_tool_calls(messages, marker_id)
        # ONE aupdate_state commit: [placeholders..., marker] — the marker is
        # appended after a provider-valid element (verdict FACT A instruction).
        # as_node="model" is REQUIRED on create_agent graphs: LangGraph cannot
        # infer the attribution node on a bare graph's state-only checkpoint
        # (next-node inference is ambiguous -> InvalidUpdateError "Ambiguous
        # update, specify as_node"); explicit attribution to the model node
        # (langchain create_agent registers it as "model",
        # agents/factory.py:1476) is deterministic regardless of where the
        # cancel landed.
        await graph.aupdate_state(config, {"messages": [*placeholders, marker]}, as_node="model")
        logger.info(
            "interrupt_marker: marker {!r} written to checkpointer "
            "(reason={!r}, partial_len={}, healed_calls={})",
            marker_id,
            reason,
            len(partial_text),
            len(placeholders),
        )
        await _persist_to_mesmemory(session_id, reason, partial_text, marker_id)

    await _void_claimed_rows(queue, session_id)


def _heal_trailing_tool_calls(messages: list[BaseMessage], marker_id: str) -> list[ToolMessage]:
    """Synthesize error ToolMessages for the TRAILING incomplete super-step.

    Decision (per ``.omo/evidence/task-3-spike-verdict.md``): heal at WRITE
    time, in the SAME ``aupdate_state`` commit as the marker. Verdict FACT B1
    shows relying on input-time ``ToolCallNormalize`` healing is NOT safe —
    its span scan silently DROPS the next HumanMessage when the dangling span
    reaches end-of-transcript (pre-existing P1, out of scope) — and FACT B3
    shows the marker only rescues that case by accident of its message type.
    Only the TRAILING incomplete super-step is healed; earlier dangling spans
    stay untouched for provider validity (appending a ToolMessage at the end
    can only answer the last AIMessage's calls anyway).
    """
    from pub_func.transcript_repair import make_missing_tool_result

    last_ai_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            last_ai_idx = i
            break
    if last_ai_idx is None:
        return []

    trailing_ai = messages[last_ai_idx]
    tool_calls = getattr(trailing_ai, "tool_calls", None) or []
    # id -> tool name, for error placeholder labeling.
    call_names: dict[str, str | None] = {
        tc["id"]: tc.get("name") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")
    }
    if not call_names:
        return []

    answered: set[str] = set()
    for m in messages[last_ai_idx + 1 :]:
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None) in call_names:
            answered.add(m.tool_call_id)

    placeholders: list[ToolMessage] = []
    for call_id in call_names:
        if call_id in answered:
            continue
        placeholder = make_missing_tool_result(call_id, call_names[call_id])
        # Deterministic placeholder id: the id-keyed reducer upserts it, so a
        # retried reconciliation never duplicates the heal (mirrors FACT D).
        placeholder.id = f"{marker_id}-heal-{call_id}"
        placeholders.append(placeholder)
    return placeholders


async def _persist_to_mesmemory(
    session_id: str, reason: InterruptReason, partial_text: str, marker_id: str
) -> None:
    """Mirror the marker as one ``role=ai`` MesMemory row (existing write path).

    Content prefix ``[interrupted:{reason}] `` carries the flag WITHOUT a
    schema change (plan Task 6 approved fallback; metadata does not survive
    into MesMemory rows). ``add_messages`` is APPEND-ONLY with NO id dedupe
    (verdict FACT D clarification), so dedupe happens HERE: the session's
    latest turn rows are scanned for an existing interrupted ai row first.
    """
    from context_engine.store import core as store_core

    prefix = _MESMEMORY_PREFIX_TEMPLATE.format(reason=reason)
    try:
        rows = store_core.get_messages_by_lastest_n_turns(session_id, last_n=2)
    except Exception as e:  # noqa: BLE001 - read failure must not abort the dual-write
        logger.warning(
            "interrupt_marker: MesMemory dedupe scan failed (writing anyway): "
            "session_id={!r}, error={!r}",
            session_id,
            e,
        )
        rows = []

    for row in rows:
        if row.get("role") == "ai" and isinstance(row.get("content"), str) and row["content"].startswith(prefix):
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
    (Task 5's durable "turn in progress" fact) must not survive as busy —
    that would block fresh turns for 24h (issues.md, Task 5 P2). QUEUED rows
    stay queued: Task 7's drain delivers them after the next turn.
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
