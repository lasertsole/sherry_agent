"""The agent-framework port the server drives a session's transcript through.

The server owns *when* a turn happens; it must not own the LangGraph checkpoint
shape. Reading the live message list, healing a provider-invalid trailing
super-step, and appending the interrupted-turn marker are all framework work, so
they live here and ``server/`` calls these domain operations instead of doing
``snapshot.values["messages"]`` surgery on a compiled graph.

Two boundaries stay with the server on purpose:

* constructing a turn's INPUT (``HumanMessage`` / ``Command(resume=...)``) — that
  is the agent's documented call contract, not checkpoint surgery;
* the auxiliary heartbeat agent, which builds its own throwaway graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import light
    from langgraph.graph.state import CompiledStateGraph

__all__ = [
    "InterruptMarkerWrite",
    "heal_interrupted_turn",
    "heal_trailing_tool_calls",
    "read_interrupt",
    "read_messages",
]

InterruptReason = Literal["cancelled", "heartbeat_timeout"]


class InterruptMarkerWrite:
    """What :func:`heal_interrupted_turn` did (the caller persists the rest)."""

    __slots__ = ("marker_id", "wrote_checkpointer", "healed_calls")

    def __init__(self, marker_id: str, wrote_checkpointer: bool, healed_calls: int) -> None:
        self.marker_id = marker_id
        self.wrote_checkpointer = wrote_checkpointer
        self.healed_calls = healed_calls


async def _resolve_graph(
    graph: CompiledStateGraph[Any, Any, Any, Any] | None,
) -> tuple[CompiledStateGraph[Any, Any, Any, Any], bool]:
    """Return ``(graph, leased)`` — a fresh lease when the caller passed none."""
    if graph is not None:
        return graph, False
    from agent import core as agent_core

    leased = await agent_core.built_agent()
    # Lease it for this operation: a concurrent rebuild replaces the cached
    # graph and closes the replaced one, which would pull the checkpointer out
    # from under this read/write.
    agent_core.hold_agent(leased)
    return leased, True


async def read_messages(
    session_id: str,
    graph: CompiledStateGraph[Any, Any, Any, Any] | None = None,
) -> list[BaseMessage]:
    """The session's live message list (post-compaction, exactly as the model sees it).

    ``graph`` is a test seam; production passes nothing and gets a leased graph.
    """
    from pub.func import build_agent_config

    resolved, leased = await _resolve_graph(graph)
    try:
        snapshot = await resolved.aget_state(config=build_agent_config(session_id))
    finally:
        if leased:
            from agent import core as agent_core

            agent_core.release_agent(resolved)
    return list((snapshot.values or {}).get("messages", []))


async def read_interrupt(
    session_id: str,
    graph: CompiledStateGraph[Any, Any, Any, Any] | None = None,
) -> dict[str, Any] | None:
    """The first pending HITL interrupt's payload, or ``None``.

    Scans EVERY task of the superstep: a graph that resumed several parallel
    branches carries one task each, and stopping at the first task silently
    drops the approval of a later one (the frontend then showed no dialog at
    all). The first interrupt with a payload wins. ``graph`` is a test seam.
    """
    from pub.func import build_agent_config

    if graph is None:
        from agent import core as agent_core

        async with agent_core.agent_lease() as leased:
            state = await leased.aget_state(config=build_agent_config(session_id))
    else:
        state = await graph.aget_state(config=build_agent_config(session_id))

    for task in getattr(state, "tasks", []):
        if not (hasattr(task, "interrupts") and task.interrupts):
            continue
        for intr in task.interrupts:
            value = getattr(intr, "value", None)
            if not isinstance(value, dict):
                continue
            action_requests = value.get("action_requests", [])
            review_configs = value.get("review_configs", [])
            if not action_requests:
                continue
            ar = action_requests[0]
            rc = review_configs[0] if review_configs else {}
            return {
                "tool_name": ar.get("name", "unknown"),
                "tool_args": ar.get("args", {}),
                "description": ar.get("description", ""),
                "allowed_decisions": rc.get("allowed_decisions", []),
            }
    return None


async def heal_interrupted_turn(
    session_id: str,
    config: RunnableConfig,
    partial_text: str,
    reason: InterruptReason,
    graph: CompiledStateGraph[Any, Any, Any, Any] | None = None,
) -> InterruptMarkerWrite:
    """Reconcile the checkpoint transcript of an interrupted turn (one commit).

    Heals the TRAILING incomplete model super-step and appends the interrupt
    marker ``AIMessage`` in the SAME ``aupdate_state`` commit. The marker
    carries a DETERMINISTIC message id
    (``interrupted-{thread_id}-{turn_seq}``), so the ``add_messages`` reducer
    upserts it — a retried write is an idempotent rewrite, never a duplicate.
    """
    resolved, leased = await _resolve_graph(graph)
    try:
        return await _heal_with_graph(resolved, session_id, config, partial_text, reason)
    finally:
        if leased:
            from agent import core as agent_core

            agent_core.release_agent(resolved)


async def _heal_with_graph(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    session_id: str,
    config: RunnableConfig,
    partial_text: str,
    reason: InterruptReason,
) -> InterruptMarkerWrite:
    snapshot = await graph.aget_state(config=config)
    messages: list[BaseMessage] = list((snapshot.values or {}).get("messages", []))

    thread_id = (config.get("configurable") or {}).get("thread_id", session_id)
    # turn_seq = number of HumanMessages in the CURRENT state: stable across
    # marker-write retries (no new human turn can land in between — the turn
    # being interrupted is not finished), deterministic per thread.
    turn_seq = sum(1 for m in messages if m.type == "human")
    marker_id = f"interrupted-{thread_id}-{turn_seq}"

    if any(getattr(m, "id", None) == marker_id for m in messages):
        # Idempotent rewrite: the deterministic id is already in state — skip
        # the checkpointer write, but let the caller run its cleanup.
        logger.info(
            "interrupt_marker: marker {!r} already in state; skipping the checkpointer write "
            "(idempotent rewrite)",
            marker_id,
        )
        return InterruptMarkerWrite(marker_id, wrote_checkpointer=False, healed_calls=0)

    marker = AIMessage(
        content=f"[interrupted] {partial_text}".strip(),
        id=marker_id,
        metadata={"interrupted": True, "reason": reason},
    )
    placeholders = heal_trailing_tool_calls(messages, marker_id)
    # ONE aupdate_state commit: [placeholders..., marker] — the marker is
    # appended after a provider-valid element.
    # as_node="model" is REQUIRED on create_agent graphs: LangGraph cannot
    # infer the attribution node on a bare graph's state-only checkpoint
    # (next-node inference is ambiguous -> InvalidUpdateError "Ambiguous
    # update, specify as_node"); explicit attribution to the model node is
    # deterministic regardless of where the cancel landed — langchain's
    # create_agent registers that node under the name "model".
    await graph.aupdate_state(config, {"messages": [*placeholders, marker]}, as_node="model")
    logger.info(
        "interrupt_marker: marker {!r} written to checkpointer "
        "(reason={!r}, partial_len={}, healed_calls={})",
        marker_id,
        reason,
        len(partial_text),
        len(placeholders),
    )
    return InterruptMarkerWrite(marker_id, wrote_checkpointer=True, healed_calls=len(placeholders))


def heal_trailing_tool_calls(messages: list[BaseMessage], marker_id: str) -> list[ToolMessage]:
    """Synthesize error ToolMessages for the TRAILING incomplete super-step.

    Decision (spike verdict): heal at WRITE time, in the SAME
    ``aupdate_state`` commit as the marker. Relying on input-time
    ``ToolCallNormalize`` healing is NOT safe — its span scan silently DROPS the
    next HumanMessage when the dangling span reaches end-of-transcript
    (pre-existing behaviour, out of scope) — and the marker only rescues that
    case by accident of its message type. Only the TRAILING incomplete
    super-step is healed; earlier dangling spans stay untouched for provider
    validity (appending a ToolMessage at the end can only answer the last
    AIMessage's calls anyway).
    """
    from pub.func.transcript_repair import make_missing_tool_result

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
        # retried reconciliation never duplicates the heal.
        placeholder.id = f"{marker_id}-heal-{call_id}"
        placeholders.append(placeholder)
    return placeholders
