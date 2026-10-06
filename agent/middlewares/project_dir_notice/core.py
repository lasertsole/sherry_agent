"""ProjectDirNoticeMiddleware — tell the agent when the working directory moved.

A session's project directory can change while the agent is idle (the user picks
another folder) or mid-turn (the choice parks on ``PROJECT_DIR_PENDING`` and is
promoted at the turn boundary). The system prompt always renders the CURRENT
root, but nothing in the transcript says it changed — so the next request
arrives with the model still reasoning about the old tree.

This middleware closes that gap at SEND time. On every turn start it compares
the effective root against the root the agent was last told about
(``StateKey.PROJECT_DIR_ANNOUNCED``) and, when they differ, splices ONE notice
``AIMessage`` immediately BEFORE the turn's HumanMessage: the model reads "the
directory moved" and then the request that depends on it, in one transcript.

Coalescing is by construction — only the comparison at send time matters:

* any number of switches between two messages produce a single notice naming
  the FINAL root;
* a switch that ends where it started produces none;
* the recorded baseline advances only when a turn actually starts, so a change
  is never swallowed by a switch that was never sent.

Placement: registered as a ``before_agent`` node (it runs once per turn, before
every ``before_model`` hook), so the human message that other middlewares rely
on arriving LAST (TaskIntent's steering check, ContextEviction's
trailing-message tag) keeps its position.

The notice is a real message in state — the transcript should carry the change
(the same call the removed switch-time announcement made), and the injected
``origin`` metadata identifies it. Fail-open: any error logs and returns
``None``; a missing notice must never break a turn.
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from loguru import logger

from runtime.session.project_dir import (
    PROJECT_DIR_NOTICE_METADATA,
    announced_project_dir,
    current_project_dir,
    project_dir_notice_text,
    project_dir_source,
    record_announced_project_dir,
)

__all__ = ["ProjectDirNoticeMiddleware"]

_SESSION_SOURCE = "session"


def _notice_message(previous: str, directory: str, *, bound: bool) -> AIMessage:
    """Build the notice as an AI message tagged like the other injected carriers."""
    return AIMessage(
        content=project_dir_notice_text(previous, directory, bound=bound),
        metadata=dict(PROJECT_DIR_NOTICE_METADATA),
    )


def _insert_before_last_human(messages: list[BaseMessage], notice: AIMessage) -> list[BaseMessage]:
    """Splice *notice* in immediately before the LAST HumanMessage.

    The human message must stay last (other middlewares key on that), so the
    notice goes in front of it. A transcript without a human message (a
    resumed / carrier turn) appends the notice instead of hunting for a slot.
    """
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return [*messages[:index], notice, *messages[index:]]
    return [*messages, notice]


class ProjectDirNoticeMiddleware(AgentMiddleware):
    """``before_agent``: announce a working-directory change once, at send time."""

    def _notice_update(self, state: AgentState) -> dict[str, Any] | None:
        """Return the state update carrying the notice, or ``None`` (no-op)."""
        try:
            session_id = str((state or {}).get("session_id") or "").strip()
            if not session_id:
                return None
            directory = str(current_project_dir(session_id))
            previous = announced_project_dir(session_id)

            # First message of the session: record the baseline silently — the
            # agent is not told about a change that never happened.
            if previous is None:
                record_announced_project_dir(session_id, directory)
                return None
            if previous == directory:
                return None

            messages = list(state.get("messages") or [])
            notice = _notice_message(
                previous,
                directory,
                bound=project_dir_source(session_id) == _SESSION_SOURCE,
            )
            record_announced_project_dir(session_id, directory)
            logger.info(
                "ProjectDirNoticeMiddleware: session {} announced {} -> {}",
                session_id,
                previous,
                directory,
            )
            # Whole-list rewrite: the messages reducer only APPENDS, so the
            # notice can be positioned only by re-emitting the list.
            return {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *_insert_before_last_human(messages, notice),
                ]
            }
        except Exception:
            logger.exception("ProjectDirNoticeMiddleware: failed; continuing without the notice")
            return None

    @override
    def before_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        logger.debug("{} before_agent hook fired", type(self).__name__)
        return self._notice_update(state)

    @override
    async def abefore_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        logger.debug("{} abefore_agent hook fired", type(self).__name__)
        return self._notice_update(state)
