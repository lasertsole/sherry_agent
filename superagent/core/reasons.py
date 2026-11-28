"""Reason codes and human-readable text (prototype ``src/core/reasons.py``)."""

from __future__ import annotations

from typing import Any

from superagent.core.model import Reason

__all__ = ["REASONS", "Reason", "reason_text"]

# The prototype file was not shipped with the plan; these are the codes named
# in the numbered docs plus the admin/HTTP codes used by the Python port.
REASONS: dict[str, str] = {
    "timeout": "The evaluation timed out.",
    "policyChanged": "The Policy changed during evaluation.",
    "routed": "The message was attached to a Ticket.",
    "startedTicket": "Accepted as new work.",
    "stoppedTicket": "Stopped by a person.",
    "unavailable": "The evaluation could not finish.",
    "ignoredFromCard": "The evaluation card had no matching rule.",
    "followupOwned": "A Ticket still owns this follow-up.",
    "notAdmitted": "The sender was not admitted by Policy.",
    "filtered": "The Source filtered this message.",
    "duplicate": "The message was already stored.",
    "requeued": "The Trigger was requeued for evaluation.",
    "resumedTicket": "The Ticket was resumed by a person.",
    "retryTicket": "The Ticket was retried by a person.",
    "completed": "The work was completed.",
    "dropped": "The work was dropped.",
    "merged": "The pull request was merged.",
    "closed": "The pull request was closed.",
    "dismissed": "The problem was dismissed by a person.",
}


def reason_text(code: str, values: dict[str, Any] | None = None) -> str:
    """Return the English text read back to the agent or person."""
    template = REASONS.get(code, code)
    try:
        return template.format(**(values or {}))
    except (KeyError, IndexError):
        return template
