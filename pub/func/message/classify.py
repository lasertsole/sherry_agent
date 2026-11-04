"""Shared message classification for the transcript pipeline.

The agent injects several kinds of ``HumanMessage`` (system reminders, workspace
notices, continuation directives, task-intent steering, subagent completion
carriers, cron results). They have the HumanMessage ROLE but were never typed by
a human. The summarizer must not label them as user requests, and the turn
slicers must not treat them as turn boundaries.

The positive test is ``metadata.origin == "user"``: the server's
``origin_for_source`` stamps exactly that on real user input, while every
injected carrier uses a different origin or a provenance marker. This is
deliberately fail-closed — a message with no metadata, or with a future origin
nobody taught this module about, is not user content.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

__all__ = ["is_user_message"]


def is_user_message(message: Any) -> bool:
    """True only for a message a human actually typed.

    Real user messages carry ``{"origin": "user"}`` (set by
    ``server.service.input_queue_service.origin_for_source("user")`` and
    persisted verbatim by the message row builder). Injected HumanMessages
    carry another origin (``project_dir`` / ``git_head`` / ``todo_continuation``
    / ``task_intent`` / ``quality_gate`` / ``background_task`` / ``cron`` /
    ``<reminder source id>``) or a provenance marker, so none of them match.

    A negative check (``metadata.internal is not True``) would be unsafe: a new
    injection path that forgot the flag would be treated as user content. The
    positive check fails closed instead.

    :param message: Any message-like object with an optional ``metadata`` dict.
    :returns: ``True`` for user-authored content, ``False`` for everything else.
    """
    if not isinstance(message, HumanMessage):
        return False
    meta = getattr(message, "metadata", None) or {}
    if not isinstance(meta, dict):
        return False
    return meta.get("origin") == "user"
