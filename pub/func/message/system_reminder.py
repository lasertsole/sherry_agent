"""The system-reminder contract: dynamic context injected into the transcript.

A ``<system-reminder>`` is a message the agent layer INSERTS to give the model
live state it must know but that must NOT live in the system prompt: the todo
list, TaskFlow board, boulder status, continuity context, knowledge index.
Keeping them out of the system prompt is what keeps that prompt STABLE — a
system prompt that changes every turn invalidates the provider's prompt cache
from its first token, persona included.

The shape mirrors the workspace-notice contract
(``pub.func.message.workspace_notice``) so every reader treats the two the same
way, and it lives in ``pub`` for the same reason: the producers sit in
``agent/`` while the message pipeline (turn splitting, summarization labels,
persistence) must recognise a reminder without importing the agent layer.

Every producer stamps:

* ``origin`` — the source id (``todo_block`` / ``taskflow_status`` / …), which
  the persistence layer keeps on the row and a reader can use to tell which
  live view a line belongs to;
* ``internal`` — the project-wide marker for "injected, not user content";
* ``provenance`` — ``reminder_catalog``, so a reminder stays identifiable even
  as the source list grows.

The text is wrapped in ``<system-reminder source="…">…</system-reminder>`` so
the model can tell injected context from what the user typed; nested occurrences
of the tag inside the payload are escaped, or a todo item could close the tag
and impersonate the layer around it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "REMINDER_PROVENANCE",
    "is_system_reminder",
    "system_reminder_metadata",
    "wrap_reminder",
]

#: Provenance marker shared by every catalog-injected reminder.
REMINDER_PROVENANCE = "reminder_catalog"

_OPEN = "<system-reminder"
_CLOSE_TAG = "</system-reminder>"


def system_reminder_metadata(source_id: str) -> dict[str, Any]:
    """The metadata a reminder carries (frozen shape).

    :param source_id: Which live view the reminder renders.
    :returns: A fresh dict (callers may hand it straight to a message).
    """
    return {
        "origin": source_id,
        "internal": True,
        "provenance": REMINDER_PROVENANCE,
    }


def is_system_reminder(message: Any) -> bool:
    """True when *message* is a catalog-injected reminder.

    Matches the provenance marker only: unlike a workspace notice there are no
    legacy origins to accept (this contract is new), and treating a foreign
    ``internal`` message as a reminder would let the subagent-completion carrier
    slip into the reminder scanners.

    :param message: Any message-like object (a ``metadata`` dict is all it needs).
    :returns: ``True`` for a reminder, ``False`` for user content, a model answer
        or any other injected carrier.
    """
    meta = getattr(message, "metadata", None) or {}
    if not isinstance(meta, dict) or meta.get("internal") is not True:
        return False
    return str(meta.get("provenance") or "").strip() == REMINDER_PROVENANCE


def wrap_reminder(source_id: str, content: str) -> str:
    """Wrap *content* in the reminder tag, escaping nested occurrences.

    The payload is generated from live state (todo text, task names, note
    bodies) — any of which a user can influence. An unescaped
    ``</system-reminder>`` inside it would end the injected block early and let
    the rest read as if the layer itself had written it.
    """
    escaped = content.replace(_OPEN, f"&lt;{_OPEN[1:]}")
    escaped = escaped.replace(_CLOSE_TAG, "&lt;/system-reminder&gt;")
    return f'<system-reminder source="{source_id}">\n{escaped}\n</system-reminder>'
