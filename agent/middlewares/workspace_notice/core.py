"""WorkspaceNoticeMiddleware — tell the agent when the workspace moved under it.

Two things can change while the agent is idle, and the transcript says nothing
about either:

* the **working directory** (the user picks another folder; the choice parks on
  ``PROJECT_DIR_PENDING`` and is promoted at the turn boundary), and
* the **git branch / HEAD** of that directory (the user switches branches in the
  sidebar's git panel, checks one out in a terminal, or commits/resets outside
  the session).

The system prompt always renders the CURRENT root, but nothing in the message
list says it moved — so the next request arrives with the model still reasoning
about the old tree, or about files from a branch that is no longer checked out.

This middleware closes both gaps at SEND time. On every turn start it compares
the effective root against the root the agent was last told about
(``StateKey.PROJECT_DIR_ANNOUNCED``) and the repository's branch+HEAD against the
token the agent was last told about (``StateKey.GIT_HEAD_ANNOUNCED``); when they
differ it splices ONE notice per change immediately BEFORE the turn's
HumanMessage — the model reads "the directory moved" / "the branch moved" and
then the request that depends on it, in one transcript.

Coalescing is by construction — only the comparison at send time matters:

* any number of switches between two messages produce a single notice per kind
  naming the FINAL state;
* a switch that ends where it started produces none;
* the recorded baselines advance only when a turn actually starts, so a change is
  never swallowed by a switch that was never sent;
* a directory switch also re-bases the git token in the SAME turn (the new root
  is a different repository), and its notice names the root it is talking about.

The notices are **HumanMessages**, not AIMessages: the injected carriers that
stand for something the user/system brought into the transcript are human rows
(every one of them tagged with an explicit ``origin``), and the persistence layer
renders a human row whose origin is not ``user`` as a neutral system card instead
of a user bubble. HumanMessage is also the honest role for the places that reason
about who authored a message — a notice is not an assistant answer. Three
consumers scan human messages generically and therefore skip notices explicitly
through ``pub.func.message.workspace_notice.is_workspace_notice``: turn splitting
(summarization), the HITL headless-turn check, and the summarizer/flush
serializers that label each line by role.

Placement: registered as a ``before_agent`` node (it runs once per turn, before
every ``before_model`` hook), and the notices go in FRONT of the last human
message, so the human message that other middlewares rely on arriving LAST
(TaskIntent's steering check, ContextEviction's trailing-message tag) keeps its
position. A transcript without a human message (a resumed / carrier turn) appends
the notices instead of hunting for a slot.

The notice is a real message in state — the transcript should carry the change
(the same call the removed switch-time announcement made), and the injected
``origin`` metadata identifies it. Fail-open: any error logs and returns
``None``; a missing notice must never break a turn.
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from loguru import logger

from runtime.session.git_head import (
    GIT_HEAD_NOTICE_METADATA,
    announced_git_head,
    git_head_notice_text,
    read_git_head,
    record_announced_git_head,
)
from runtime.session.project_dir import (
    PROJECT_DIR_NOTICE_METADATA,
    announced_project_dir,
    current_project_dir,
    project_dir_notice_text,
    project_dir_source,
    record_announced_project_dir,
)

__all__ = ["WorkspaceNoticeMiddleware"]

_SESSION_SOURCE = "session"


def _notice(metadata: dict[str, Any], text: str) -> HumanMessage:
    """Build one notice as a human message tagged with its notice metadata."""
    return HumanMessage(content=text, metadata=dict(metadata))


def _insert_before_last_human(
    messages: list[BaseMessage], notices: list[HumanMessage]
) -> list[BaseMessage]:
    """Splice *notices* in immediately before the LAST HumanMessage, in order.

    The human message must stay last (other middlewares key on that), so the
    notices go in front of it. A transcript without a human message (a resumed /
    carrier turn) appends them instead of hunting for a slot.
    """
    if not notices:
        return messages
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return [*messages[:index], *notices, *messages[index:]]
    return [*messages, *notices]


class WorkspaceNoticeMiddleware(AgentMiddleware):
    """``before_agent``: announce directory / git changes once, at send time."""

    def _notice_update(self, state: AgentState) -> dict[str, Any] | None:
        """Return the state update carrying the notices, or ``None`` (no-op)."""
        try:
            session_id = str((state or {}).get("session_id") or "").strip()
            if not session_id:
                return None
            directory = str(current_project_dir(session_id))
            notices: list[HumanMessage] = []
            switched = False

            # Working directory: the first message of a session records the
            # baseline silently — the agent is not told about a change that never
            # happened.
            previous = announced_project_dir(session_id)
            if previous is None:
                record_announced_project_dir(session_id, directory)
            elif previous != directory:
                notices.append(
                    _notice(
                        PROJECT_DIR_NOTICE_METADATA,
                        project_dir_notice_text(
                            previous,
                            directory,
                            bound=project_dir_source(session_id) == _SESSION_SOURCE,
                        ),
                    )
                )
                record_announced_project_dir(session_id, directory)
                switched = True

            # Git branch/HEAD of that directory. ``None`` covers "not a
            # repository", "no git binary" and a failed read alike: nothing to
            # announce, and the baseline stays unset so a repository that appears
            # later still starts from a silent baseline.
            token = read_git_head(directory)
            if token:
                previous_head = announced_git_head(session_id)
                if previous_head is None:
                    record_announced_git_head(session_id, token)
                elif previous_head != token:
                    notices.append(
                        _notice(
                            GIT_HEAD_NOTICE_METADATA,
                            git_head_notice_text(
                                previous_head, token, directory, switched=switched
                            ),
                        )
                    )
                    record_announced_git_head(session_id, token)

            if not notices:
                return None
            messages = list(state.get("messages") or [])
            logger.info(
                "WorkspaceNoticeMiddleware: session {} announced {} change(s) (root {}, head {})",
                session_id,
                len(notices),
                directory,
                token or "-",
            )
            # Whole-list rewrite: the messages reducer only APPENDS, so the
            # notices can be positioned only by re-emitting the list.
            return {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *_insert_before_last_human(messages, notices),
                ]
            }
        except Exception:
            logger.exception("WorkspaceNoticeMiddleware: failed; continuing without the notice")
            return None

    @override
    def before_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        logger.bind(middleware=type(self).__name__).debug("before_agent hook fired")
        return self._notice_update(state)

    @override
    async def abefore_agent(self, state: AgentState, runtime: Any = None) -> dict[str, Any] | None:
        logger.bind(middleware=type(self).__name__).debug("abefore_agent hook fired")
        return self._notice_update(state)
