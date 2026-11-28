"""Record rows for panel tables (prototype ``src/app/views/records.ts``)."""

from __future__ import annotations

from typing import Any

from superagent.app.tools.cards import ticket_card, trigger_card
from superagent.core.model import Ticket, Trigger


def trigger_row(trigger: Trigger) -> dict[str, Any]:
    """Return one Trigger row."""
    card = trigger_card(trigger)
    return {**card, "source": trigger.source, "threads": [ref.id for ref in trigger.threads]}


def ticket_row(ticket: Ticket) -> dict[str, Any]:
    """Return one Ticket row."""
    return {**ticket_card(ticket), "ended": ticket.ended_at}


def owners_of(trigger_id: str, tickets: list[Ticket]) -> list[str]:
    """Return Ticket ids owning a follow-up Trigger."""
    return [
        ticket.id
        for ticket in tickets
        if trigger_id == ticket.origin.get("trigger_id")
        or any(item.trigger_id == trigger_id for item in ticket.inputs)
    ]
