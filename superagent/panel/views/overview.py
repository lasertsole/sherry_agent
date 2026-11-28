"""Overview view (prototype ``src/app/views/overview.ts``)."""

from __future__ import annotations

from typing import Any

from superagent.core.model import Ticket
from superagent.core.stage import TicketStage, ticket_stage


def overview(
    tickets: list[Ticket],
    health: dict[str, dict[str, Any]],
    load: dict[str, int],
    setup: dict[str, Any],
) -> dict[str, Any]:
    """Return the three-question Overview payload."""
    attention = [
        ticket.id
        for ticket in tickets
        if ticket_stage(ticket)
        in (
            TicketStage.PERSON_ANSWER,
            TicketStage.PERSON_REVIEW,
            TicketStage.PERSON_BLOCKED_FAILED,
        )
    ]
    problems = {name: value for name, value in health.items() if value.get("state") == "error"}
    return {
        "needs_attention": attention,
        "problems": problems,
        "setup": setup,
        "load": load,
        "activity_24h": {"triggers": 0, "decisions": 0, "tickets": len(tickets)},
    }
