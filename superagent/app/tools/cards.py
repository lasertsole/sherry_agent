"""Compact cards for assessment and panel views (prototype ``src/app/cards.py``)."""

from __future__ import annotations

from superagent.core.model import Ticket, Trigger
from superagent.limits import MAX_REPORT_SUMMARY


def trigger_card(trigger: Trigger) -> dict[str, str]:
    """Return a compact assessment card for a Trigger."""
    return {
        "id": trigger.id,
        "at": trigger.at,
        "text": trigger.text[:4000],
        "actor": trigger.actor.id,
    }


def ticket_card(ticket: Ticket) -> dict[str, str]:
    """Return a compact card for a Ticket."""
    summary = ticket.latest_report.summary if ticket.latest_report else ""
    return {
        "id": ticket.id,
        "stage": summary[:MAX_REPORT_SUMMARY],
        "source": ticket.origin.get("trigger_id", ""),
    }
