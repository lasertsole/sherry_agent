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

from .core import clear_session, resolve_injections

__all__ = ["DynamicReminderMiddleware"]

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


class DynamicReminderMiddleware(AgentMiddleware):
    """Inject the live ``<system-reminder>`` layer once per turn."""

    async def abefore_model(self, state, runtime=None) -> dict[str, Any] | None:
        from agent.middlewares.agent_switch import middleware_enabled

        if not middleware_enabled(str((state or {}).get("session_id") or ""), type(self).__name__):
            return None  # No-op hook while the preset Middlewares tab disables this middleware.
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

            injections = resolve_injections(session_id, state, current_turn=turn)
            if not injections:
                return None

            reminders = [
                HumanMessage(
                    content=wrap_reminder(source_id, content),
                    metadata=system_reminder_metadata(source_id),
                )
                # resolve_injections returns high→low (its descending sort is
                # for budget allocation). Reverse for delivery so the highest
                # priority view — plan mode, then the todo list — lands at the
                # END of the reminder block, immediately BEFORE the user message
                # (the closest position to the generation point that still keeps
                # the user's own input last).
                for source_id, content in reversed(list(injections.items()))
            ]
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
                    *messages[:-1],
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


def reset_session(session_id: str) -> None:
    """Drop one session's reminder bookkeeping (compression / teardown).

    Called after a compression rebuilt the system prompt: the older reminders
    may have been compressed away, so the next turn must re-inject every source
    from live state instead of trusting the change-detection hashes.
    """
    clear_session(session_id)
    _TURN_COUNTS.pop(session_id, None)
