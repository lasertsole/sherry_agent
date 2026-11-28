"""Ticket views (prototype ``src/app/views/tickets.ts``)."""

from __future__ import annotations

from superagent.core.model import Ticket
from superagent.core.stage import TicketStage, ticket_stage
from superagent.panel.views.records import ticket_row


def by_stage(tickets: list[Ticket]) -> dict[str, list[dict[str, object]]]:
    """Group open Tickets by their stage."""
    groups: dict[str, list[dict[str, object]]] = {stage.value: [] for stage in TicketStage}
    for ticket in tickets:
        stage = ticket_stage(ticket)
        if stage is not None:
            groups[stage.value].append(ticket_row(ticket))
    return groups


def detail(ticket: Ticket) -> dict[str, object]:
    """Return a Ticket detail view."""
    return {
        **ticket_row(ticket),
        "authorization": ticket.mandate.__dict__,
        "inputs": [item.text for item in ticket.inputs],
        "assignments": [item.id for item in ticket.assignments],
        "follows": [item.thread.id for item in ticket.follows],
    }


__all__ = ["by_stage", "detail"]
