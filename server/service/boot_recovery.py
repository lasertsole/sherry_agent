"""Boot-time reconciliation of state that lives only in memory.

Three facts die with the process and each has a boot-time repair:

1. **HITL pending** — ``set_hitl_pending`` writes a memory register only, so a
   restart loses it. The graph is still SUSPENDED in its checkpoint, but the
   server reports IDLE; the next user message is then dispatched as an ordinary
   turn and the ``Command(resume=...)`` contract is broken. Rebuilding the flag
   from the checkpoint is the only way to see it.
2. **A dangling tool call** — a turn killed mid-tool leaves an ``AIMessage``
   with ``tool_calls`` and no ``ToolMessage``. Replaying that transcript to a
   provider is invalid, and the tool result can never arrive. Healing writes
   the error placeholders plus the interrupt marker in one commit, which is
   exactly what the cancel path does.
3. **A CLAIMED queue row** — a queued user message claimed by a turn that never
   finished. No turn survives a restart, so a CLAIMED row is orphaned by
   definition; requeueing it preserves the user's message for the next turn.

Every step is FAIL-OPEN and bounded: a session that cannot be read is skipped,
and the scan is capped so a large install cannot turn startup into a minute of
graph builds. The whole pass runs in a thread-free sequence before serving.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

#: Sessions examined per boot pass. Each candidate costs ONE leased graph build
#: (that is the only way to read a suspended interrupt), so the cap keeps a
#: large install's startup bounded; a session outside the cap is repaired by
#: ``submit_user_input``'s per-call busy probe instead.
_MAX_SESSIONS = 50
#: Sessions per batch — the pass yields to the loop between batches so a boot
#: that is racing a health probe still answers.
_BATCH_SIZE = 10


def candidate_session_ids() -> list[str]:
    """Session ids that may hold a live checkpoint, newest directory first.

    The session directory is the only durable index of session ids (checkpoint
    thread ids are hashes and cannot be inverted). A directory without a
    checkpoint row is skipped without building a graph.
    """
    from config.path import SESSIONS_DIR

    from .session_dir_sweep import _checkpoints_has_session

    base = Path(SESSIONS_DIR)
    if not base.is_dir():
        return []
    try:
        entries = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        logger.debug("boot recovery: session dir listing failed", exc_info=True)
        return []
    entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    ids: list[str] = []
    for entry in entries:
        if len(ids) >= _MAX_SESSIONS:
            break
        session_id = entry.name
        # Child sessions and compression forks are not user-facing sessions:
        # they take no queued input, and the subagent registry owns their
        # restart reconciliation, so a graph build here would be pure cost.
        if ":subagent:" in session_id or "::" in session_id:
            continue
        try:
            if _checkpoints_has_session(session_id):
                ids.append(session_id)
        except Exception:  # noqa: BLE001 - a probe failure must not stop boot
            continue
    return ids


async def rebuild_hitl_pending() -> dict[str, int]:
    """Re-mark sessions whose checkpoint carries a pending HITL interrupt.

    Returns ``{"scanned": n, "rebuilt": m}``. Fail-open per session: a graph
    that cannot be leased is skipped, and the runtime busy probe in
    ``submit_user_input`` still protects that session on its next message.
    """
    from agent.state_port import read_interrupt
    from agent.tools.subagent.registry.session_state import set_hitl_pending

    scanned = 0
    rebuilt = 0
    sessions = candidate_session_ids()
    for index, session_id in enumerate(sessions):
        if index and index % _BATCH_SIZE == 0:
            import asyncio

            await asyncio.sleep(0)
        scanned += 1
        try:
            pending = await read_interrupt(session_id)
        except Exception:  # noqa: BLE001 - optional probe; never block startup
            logger.debug("boot recovery: interrupt probe failed for {}", session_id, exc_info=True)
            continue
        if pending is None:
            continue
        try:
            set_hitl_pending(session_id, True)
            rebuilt += 1
            logger.info(
                "boot recovery: session {} has a pending HITL interrupt ({}); marked busy",
                session_id,
                pending.get("tool_name"),
            )
        except Exception:  # noqa: BLE001
            logger.debug("boot recovery: set_hitl_pending failed for {}", session_id, exc_info=True)
    return {"scanned": scanned, "rebuilt": rebuilt}


async def heal_dangling_turns() -> int:
    """Heal transcripts left with a trailing tool call and no result.

    A session with a pending HITL interrupt is skipped: it is legitimately
    suspended, and a marker would be written over a live resume target.
    """
    from agent.state_port import heal_interrupted_turn, read_messages
    from agent.tools.subagent.registry.session_state import is_hitl_pending
    from pub.func import build_agent_config

    healed = 0
    for session_id in candidate_session_ids():
        try:
            if is_hitl_pending(session_id):
                continue
            messages = await read_messages(session_id)
            if not _has_dangling_tool_call(messages):
                continue
            write = await heal_interrupted_turn(
                session_id,
                build_agent_config(session_id),
                "",
                "cancelled",
            )
        except Exception:  # noqa: BLE001 - optional probe; never block startup
            logger.debug("boot recovery: heal failed for {}", session_id, exc_info=True)
            continue
        if write.healed_calls:
            healed += 1
            logger.info(
                "boot recovery: healed {} dangling tool call(s) in session {}",
                write.healed_calls,
                session_id,
            )
    return healed


def _has_dangling_tool_call(messages: list) -> bool:
    """True when the transcript ends with tool calls that have no results.

    Only the TRAILING super-step matters: an answered call earlier in the
    transcript is history, not a dangling call. OpenAI-shaped provider messages
    are ``AIMessage.tool_calls`` on the assistant turn and ``ToolMessage``
    results after it.
    """
    answered = {
        str(getattr(msg, "tool_call_id", "") or "")
        for msg in messages
        if getattr(msg, "type", "") == "tool"
    }
    for msg in reversed(messages):
        if getattr(msg, "type", "") == "human":
            return False  # a later human turn already closed the span
        calls = getattr(msg, "tool_calls", None)
        if not calls:
            continue
        call_ids = [str(call.get("id") or "") for call in calls if isinstance(call, dict)]
        return any(call_id and call_id not in answered for call_id in call_ids)
    return False


async def requeue_claimed_inputs() -> int:
    """Requeue queue rows a crashed turn had claimed (none can be running now).

    A CLAIMED row means a turn took the message and never finished it: after a
    restart no turn exists, so the row would otherwise hold its slot until the
    24-hour expiry. Requeueing keeps the user's message for the next turn
    instead of voiding it.
    """
    from server.queue.user_input_queue import UserInputQueue

    queue = UserInputQueue()
    total = 0
    for session_id in candidate_session_ids():
        try:
            total += await queue.requeue_claimed(session_id)
        except Exception:  # noqa: BLE001 - optional probe; never block startup
            logger.debug("boot recovery: queue requeue failed for {}", session_id, exc_info=True)
    return total


async def reconcile_at_boot() -> dict[str, int]:
    """Run the three repairs in order (HITL first, then heal, then the queue)."""
    result: dict[str, int] = {"scanned": 0, "hitl_rebuilt": 0, "turns_healed": 0, "requeued": 0}
    try:
        hitl = await rebuild_hitl_pending()
        result["scanned"] = hitl["scanned"]
        result["hitl_rebuilt"] = hitl["rebuilt"]
    except Exception:  # noqa: BLE001 - boot recovery is best-effort
        logger.exception("boot recovery: HITL rebuild failed")
    try:
        result["turns_healed"] = await heal_dangling_turns()
    except Exception:  # noqa: BLE001
        logger.exception("boot recovery: turn healing failed")
    try:
        result["requeued"] = await requeue_claimed_inputs()
    except Exception:  # noqa: BLE001
        logger.exception("boot recovery: queue requeue failed")
    if result["hitl_rebuilt"] or result["turns_healed"] or result["requeued"]:
        logger.info(
            "boot recovery: {} session(s) scanned, {} HITL flag(s) rebuilt, "
            "{} turn(s) healed, {} queued message(s) requeued",
            result["scanned"],
            result["hitl_rebuilt"],
            result["turns_healed"],
            result["requeued"],
        )
    return result
