"""Domain event → main Session notice/relay mapping (prototype ``src/app/notices.ts``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from superagent.core.model import Report, Ticket, Trigger
from superagent.core.ports import DomainEvent, DomainEvents


@dataclass(frozen=True)
class Notice:
    """A non-waking main Session notice."""

    text: str
    event: str


@dataclass(frozen=True)
class Relay:
    """A wake-up delivery to the main Session."""

    text: str
    kind: str
    ticket_id: str | None = None


def event_notice(event: DomainEvent) -> Notice | None:
    """Map durable domain events to quiet notices."""
    if event.name in {
        DomainEvents.TICKET_ENDED,
        DomainEvents.TRIGGER_REQUEUED,
        DomainEvents.THREAD_UNSUBSCRIBED,
        DomainEvents.POLICY_CHANGED,
    }:
        return Notice(text=f"{event.name}: {event.payload}", event=event.name)
    return None


def report_relay(report: Report, ticket: Ticket) -> Relay | None:
    """Forward questions and results; keep progress in the Ticket."""
    if report.question:
        return Relay(text=report.question, kind="attention", ticket_id=ticket.id)
    if report.kind.value in {"ready", "blocked", "failed", "dropped"}:
        return Relay(
            text=f"{report.kind.value}: {report.summary}", kind="result", ticket_id=ticket.id
        )
    return None


def followup_relay(trigger: Trigger, tickets: list[Ticket]) -> Relay | None:
    """Describe a person follow-up and its owning Tickets."""
    if not trigger.human:
        return None
    owners = ", ".join(ticket.id for ticket in tickets) or "no Ticket"
    return Relay(text=f"{trigger.text} (owners: {owners})", kind="followup")


def result_relay(ticket: Ticket, text: str, **extra: Any) -> Relay:
    """Build a result relay for a Ticket."""
    del extra
    return Relay(text=text, kind="result", ticket_id=ticket.id)


__all__ = ["Notice", "Relay", "event_notice", "followup_relay", "report_relay", "result_relay"]
