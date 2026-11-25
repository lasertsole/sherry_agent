"""``before_model``: inject the reminder catalogue into the transcript.

Registered immediately AFTER ``TaskIntentMiddleware`` (whose per-turn dedup
requires the user message to still be LAST when it runs) and BEFORE the
middlewares that do not care about trailing injected messages.

The hook injects on the turn's FIRST model call only — the same "the last
message is a genuine user message" test ``TaskIntentMiddleware`` uses — so a
tool-loop continuation inside one turn cannot re-inject. The reminders are
positioned IMMEDIATELY BEFORE the user's message (not after it), so the input
reads as "live state first, then the request" while the human message stays
last for the middlewares downstream. Failure is fail-open: a broken builder or
an unreadable register costs a reminder, never a turn.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from loguru import logger

from pub.func.message.system_reminder import (
    is_system_reminder,
    system_reminder_metadata,
    wrap_reminder,
)

from .core import (
    aresolve_injections,
    clear_session,
    registered_sources,
    transient_source_ids,
)

__all__ = ["DynamicReminderMiddleware", "capture_reminder_snapshot"]

#: ``{session_id: turn_count}`` — the monotonic per-session turn counter the
#: cooldowns are measured in. One increment per injected-or-attempted turn
#: (i.e. per first model call), never per model call inside a turn.
_TURN_COUNTS: dict[str, int] = {}


def _is_injected(msg: Any) -> bool:
    """True for every internally injected carrier (not user content)."""
    meta = getattr(msg, "metadata", None) or {}
    if isinstance(meta, dict) and meta.get("internal"):
        return True
    content = getattr(msg, "content", None)
    return isinstance(content, str) and content.startswith("[SYSTEM DIRECTIVE")


def _already_injected_this_turn(messages: list[Any]) -> bool:
    """Whether a reminder block from THIS turn already sits before the user.

    Reminders are placed before the user message, so after a crash between the
    injection and the model call the resumed state again ends with the user
    message. Scanning back to the previous assistant turn finds the already
    injected block and prevents a second copy.
    """
    for message in reversed(messages[:-1]):
        if isinstance(message, AIMessage):
            return False
        if is_system_reminder(message):
            return True
    return False


def _is_transient_reminder(msg: Any, transient_ids: frozenset[str]) -> bool:
    """True when *msg* is a reminder for a transient source."""
    if not transient_ids or not is_system_reminder(msg):
        return False
    meta = getattr(msg, "metadata", None) or {}
    return meta.get("origin") in transient_ids


class DynamicReminderMiddleware(AgentMiddleware):
    """Inject the live ``<system-reminder>`` layer once per turn."""

    async def abefore_model(self, state, runtime=None) -> dict[str, Any] | None:
        try:
            messages = state.get("messages") if isinstance(state, dict) else None
            if not messages:
                return None

            # First model call of the turn only: the last message must be the
            # user's own message (an injected reminder / notice / directive does
            # not count as one).
            final = messages[-1]
            last_human = None
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage) and not _is_injected(msg):
                    last_human = msg
                    break
            if last_human is None or last_human is not final:
                return None
            if _already_injected_this_turn(list(messages)):
                return None

            session_id = str(state.get("session_id") or "")
            if not session_id:
                return None

            turn = _TURN_COUNTS.get(session_id, 0) + 1
            _TURN_COUNTS[session_id] = turn

            transient_ids = transient_source_ids()
            sources = registered_sources()
            injections = await aresolve_injections(session_id, state, current_turn=turn)

            reminders = [
                HumanMessage(
                    content=wrap_reminder(source_id, content),
                    metadata=system_reminder_metadata(
                        source_id,
                        compress_exclude=(
                            sources[source_id].compress_exclude if source_id in sources else True
                        ),
                    ),
                )
                # resolve_injections returns high→low (its descending sort is
                # for budget allocation). Reverse for delivery so the highest
                # priority view — plan mode, then the todo list — lands at the
                # END of the reminder block, immediately BEFORE the user message
                # (the closest position to the generation point that still keeps
                # the user's own input last).
                for source_id, content in reversed(list(injections.items()))
            ]

            # Strip reminders from transient sources that were injected on an
            # earlier turn. Without this, an only_if_changed source
            # (project_instructions: 8 k of AGENTS.md) would ride every turn's
            # context through the messages[:-1] re-emit.
            tail = messages[:-1]
            stripped = False
            if transient_ids:
                filtered = [msg for msg in tail if not _is_transient_reminder(msg, transient_ids)]
                stripped = len(filtered) < len(tail)
                tail = filtered

            if not injections and not stripped:
                return None

            logger.debug(
                "DynamicReminderMiddleware: injecting {} reminder(s) for session {} (turn {}): {}",
                len(reminders),
                session_id,
                turn,
                ", ".join(injections),
            )
            # The reminders belong BEFORE the user's message: the input reads
            # as "live state first, then the request". The reducer only appends,
            # so this positions them with a whole-list rewrite (same technique
            # WorkspaceNoticeMiddleware uses for its notices). The human message
            # therefore stays LAST, which the middlewares downstream rely on.
            return {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *tail,
                    *reminders,
                    messages[-1],
                ]
            }
        except Exception:
            logger.exception("DynamicReminderMiddleware: failed; continuing without reminders")
            return None

    def before_model(self, state, runtime=None) -> dict[str, Any] | None:
        """Sync best-effort path (production main agent streams async-only)."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(self.abefore_model(state, runtime))
            except Exception:
                logger.exception("DynamicReminderMiddleware (sync) failed")
                return None
        logger.debug("DynamicReminderMiddleware: running loop detected; async-only path skips")
        return None


def capture_reminder_snapshot(messages: list[Any], preserved: list[Any]) -> list[Any]:
    """Capture the last non-transient reminder per source from a cutoff window.

    Called by compression just after it replaces the cutoff window with a
    summary pair. Without this, every ``<system-reminder>`` that lived inside
    the window (todo block, TaskFlow board, plan mode, …) disappears with it —
    the model keeps executing the remaining turn with no state anchor and drifts.

    The snapshot is the version AT COMPRESSION TIME, not live state. It is
    deliberately inserted between the summary pair and the preserved tail so
    the model sees a timeline: summary ("we did X") → snapshot ("plan was
    A/B/C then") → preserved reminders (newer, closer to the tail). Preserved
    reminders are never removed — they are newer and carry stronger attention.

    Dedup: per source, if the snapshot content is byte-identical to the OLDEST
    preserved reminder for that source, the snapshot is pure redundancy — the
    preserved copy already bridges the summary to the live state — so it is
    dropped. Compare to the OLDEST (not newest) so a state that changed and
    changed back is not mistaken for "no change".

    Transient sources (current_time, lsp_diagnostics, project_instructions) are
    never captured: they are re-derived each turn and a stale time/notice would
    mislead. Failure is fail-open — an empty list just means no snapshot.

    :param messages: The cutoff window being summarized.
    :param preserved: Messages kept after the cutoff.
    :returns: Captured reminder messages in their original order (highest
        injection priority last, matching delivery order).
    """
    try:
        transient_ids = transient_source_ids()

        # 1. Last reminder per non-transient source in the cutoff window.
        latest_by_origin: dict[str, Any] = {}
        for msg in messages:
            if not is_system_reminder(msg):
                continue
            meta = getattr(msg, "metadata", None) or {}
            origin = meta.get("origin")
            if origin is None or origin in transient_ids:
                continue
            latest_by_origin[origin] = msg

        if not latest_by_origin:
            return []

        # 2. Oldest preserved reminder per source (the first after the summary).
        oldest_preserved_by_origin: dict[str, Any] = {}
        for msg in preserved:
            if not is_system_reminder(msg):
                continue
            meta = getattr(msg, "metadata", None) or {}
            origin = meta.get("origin")
            if origin is None or origin in transient_ids:
                continue
            if origin not in oldest_preserved_by_origin:
                oldest_preserved_by_origin[origin] = msg

        def _content(msg: Any) -> str:
            value = getattr(msg, "content", "")
            return value if isinstance(value, str) else str(value)

        # 3. Skip a snapshot whose content equals the oldest preserved copy.
        skipped: set[str] = set()
        for origin, snapshot_msg in latest_by_origin.items():
            oldest = oldest_preserved_by_origin.get(origin)
            if oldest is not None and _content(snapshot_msg) == _content(oldest):
                skipped.add(origin)

        # 4. Re-emit the LAST message per surviving source, in original order.
        result: list[Any] = []
        emitted: set[str] = set()
        for msg in messages:
            if not is_system_reminder(msg):
                continue
            origin = (getattr(msg, "metadata", None) or {}).get("origin")
            if (
                origin is None
                or origin in emitted
                or origin in skipped
                or latest_by_origin.get(origin) is not msg
            ):
                continue
            result.append(msg)
            emitted.add(origin)
        return result
    except Exception:  # noqa: BLE001 - compression must never fail on a snapshot
        logger.debug("capture_reminder_snapshot failed; continuing without snapshot")
        return []


def reset_session(session_id: str) -> None:
    """Drop one session's reminder bookkeeping (compression / teardown).

    Called after a compression rebuilt the system prompt: the older reminders
    may have been compressed away, so the next turn must re-inject every source
    from live state instead of trusting the change-detection hashes.
    """
    clear_session(session_id)
    _TURN_COUNTS.pop(session_id, None)
