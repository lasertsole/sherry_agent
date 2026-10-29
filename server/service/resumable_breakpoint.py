"""The resumable breakpoint of an interrupted turn.

When a turn is cut off mid-answer (user Stop or the heartbeat idle timeout) the
transcript itself is still resumable: ``heal_interrupted_turn`` left a marker
``AIMessage("[interrupted] {partial}")`` at the end of the checkpoint. This
module records the tiny bit of identity a continuation needs, so the user can
press "continue" instead of re-sending the question:

* ``marker_id``    — the deterministic marker id the heal wrote;
* ``partial_text`` — the visible answer prefix (the marker content minus its
  ``[interrupted]`` prefix);
* ``reason`` / ``turn_id`` / ``turn_seq`` — provenance and the validation
  fingerprint (a later turn changes ``turn_seq``);
* ``is_reasoning_only`` — the model spent the turn thinking and produced no
  visible text, so the continuation must use the answer-now prompt.

Only PURE-TEXT interruptions get a breakpoint: when the heal had to synthesize
tool-result placeholders (``healed_calls > 0``) the turn died inside a tool
call, and what "continue" should mean there (rerun the tool? skip it?) is a
different product decision — ``healed_calls == 0`` is the gate.

Storage is the ordinary state register, dual-written (mem for the hot path, DB
for a restart). One breakpoint per session: a new turn clears it, and a
continuation clears it before streaming (F6 — one resume per interruption).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from runtime import StateKey, state_register_db, state_register_mem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langchain_core.messages import BaseMessage
    from langgraph.graph.state import CompiledStateGraph

BreakpointReason = Literal["cancelled", "heartbeat_timeout"]


class BreakpointStaleError(RuntimeError):
    """The breakpoint no longer describes the transcript's tail.

    Raised inside a continuation turn when the transcript moved between the
    request's validation and the turn's own read (or the marker vanished). The
    text is user-facing: the client shows it as the failed continuation and
    refreshes its history.
    """


__all__ = [
    "BreakpointReason",
    "BreakpointStaleError",
    "breakpoint_is_valid",
    "build_breakpoint",
    "clear_breakpoint",
    "has_breakpoint",
    "read_breakpoint",
    "set_breakpoint",
    "validate_breakpoint",
]


def _read_state(session_id: str, key: str, default: Any) -> Any:
    """Mem tier first, then the durable tier (promoting the hit back to mem)."""
    value = state_register_mem.get_state(session_id, key, None)
    if value is not None:
        return value
    value = state_register_db.get_state(session_id, key, None)
    if value is not None:
        state_register_mem.set_state(session_id, key, value)
        return value
    return default


def read_breakpoint(session_id: str) -> dict[str, Any] | None:
    """The session's resumable breakpoint, or ``None``.

    A malformed value (a stale register row from an older shape) reads as
    "nothing to continue" rather than crashing the caller.
    """
    value = _read_state(session_id, StateKey.RESUMABLE_BREAKPOINT, None)
    return value if isinstance(value, dict) else None


def has_breakpoint(session_id: str) -> bool:
    """Whether the session currently has a resumable breakpoint."""
    return read_breakpoint(session_id) is not None


def set_breakpoint(session_id: str, payload: dict[str, Any]) -> None:
    """Dual-write one breakpoint (mem for the hot path, DB for restarts)."""
    state_register_mem.set_state(session_id, StateKey.RESUMABLE_BREAKPOINT, payload)
    state_register_db.set_state(session_id, StateKey.RESUMABLE_BREAKPOINT, payload)


def clear_breakpoint(session_id: str) -> None:
    """Forget the breakpoint in both tiers (new turn / consumed / cleared)."""
    state_register_mem.delete_state(session_id, StateKey.RESUMABLE_BREAKPOINT)
    state_register_db.delete_state(session_id, StateKey.RESUMABLE_BREAKPOINT)


def build_breakpoint(
    *,
    marker_id: str,
    partial_text: str,
    reason: BreakpointReason,
    turn_id: str,
    turn_seq: int,
    is_reasoning_only: bool,
) -> dict[str, Any]:
    """Assemble the persisted payload (the shape :func:`read_breakpoint` reads)."""
    return {
        "marker_id": marker_id,
        "partial_text": partial_text,
        "reason": reason,
        "at_turn_id": turn_id,
        "turn_seq": turn_seq,
        "is_reasoning_only": is_reasoning_only,
    }


def breakpoint_is_valid(messages: list[BaseMessage], breakpoint: dict[str, Any] | None) -> bool:
    """Whether the live transcript still carries this exact breakpoint.

    The marker must still be the TRAILING message, the turn count must match
    what was recorded, and the marker itself must be present. Anything else
    means a later turn (or an edit) moved the transcript on: the partial answer
    a continuation would append to is no longer the tail, so resuming would
    produce a confusing interleaving. A stale breakpoint is dropped, not reused.

    ``turn_seq`` is counted over ALL human messages (internal notices and the
    continuation prompt included) because that is exactly how
    ``heal_interrupted_turn`` derives the marker id — the validation must speak
    the same language as the writer.
    """
    if not isinstance(breakpoint, dict):
        return False
    marker_id = breakpoint.get("marker_id")
    turn_seq = breakpoint.get("turn_seq")
    if not isinstance(marker_id, str) or not marker_id or not isinstance(turn_seq, int):
        return False
    if not messages:
        return False
    humans = sum(1 for message in messages if message.type == "human")
    if humans != turn_seq:
        return False
    return getattr(messages[-1], "id", None) == marker_id


async def validate_breakpoint(
    session_id: str,
    breakpoint: dict[str, Any] | None,
    graph: CompiledStateGraph[Any, Any, Any, Any] | None = None,
) -> bool:
    """Read the live transcript and validate the breakpoint against it.

    ``graph`` is a test seam; production passes nothing and gets a leased one.
    A read failure reads as "invalid" — the continuation then answers "stale"
    instead of writing into an unknown transcript.
    """
    from agent.state_port import read_messages

    try:
        messages = await read_messages(session_id, graph)
    except Exception as exc:  # noqa: BLE001 - fail-open on an unreadable transcript
        logger.warning("resumable_breakpoint: transcript read failed for {}: {}", session_id, exc)
        return False
    return breakpoint_is_valid(messages, breakpoint)
