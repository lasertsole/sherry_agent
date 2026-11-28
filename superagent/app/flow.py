"""Ticket ↔ Sherry TaskFlow mapping and finish gate (prototype ``src/app/flow.ts``).

A Ticket owns at most one TaskFlow.  The mapping is kept outside the Engine so
the domain layer stays free of Sherry concepts; the finish gate reuses the
delivery requirements and refuses to close a Ticket before a ready report.
"""

from __future__ import annotations

from dataclasses import dataclass

from superagent.app.delivery import AgentDelivery
from superagent.core.ids import key
from superagent.core.model import Assignment, Input, Report, ReportKind, Ticket, Trigger
from superagent.core.stage import TicketStage, ticket_stage


@dataclass(frozen=True)
class FlowBinding:
    """One Ticket-to-TaskFlow binding."""

    ticket_id: str
    flow_id: str
    session_key: str = ""


class FlowMap:
    """Bidirectional, one-to-one index of Ticket ↔ TaskFlow."""

    def __init__(self, delivery: AgentDelivery | None = None) -> None:
        self.delivery = delivery or AgentDelivery()
        self._by_ticket: dict[str, FlowBinding] = {}
        self._by_flow: dict[str, FlowBinding] = {}

    def bind(self, ticket_id: str, flow_id: str, session_key: str = "") -> FlowBinding:
        """Bind a Ticket to a TaskFlow, replacing any previous binding."""
        previous = self._by_ticket.get(ticket_id)
        if previous is not None:
            self._by_flow.pop(previous.flow_id, None)
        binding = FlowBinding(ticket_id=ticket_id, flow_id=flow_id, session_key=session_key)
        self._by_ticket[ticket_id] = binding
        self._by_flow[flow_id] = binding
        return binding

    def unbind(self, ticket_id: str) -> bool:
        """Remove a Ticket's binding."""
        binding = self._by_ticket.pop(ticket_id, None)
        if binding is None:
            return False
        self._by_flow.pop(binding.flow_id, None)
        return True

    def by_ticket(self, ticket_id: str) -> FlowBinding | None:
        """Look up a binding by Ticket id."""
        return self._by_ticket.get(ticket_id)

    def by_flow(self, flow_id: str) -> FlowBinding | None:
        """Look up a binding by TaskFlow id."""
        return self._by_flow.get(flow_id)

    def finish_allowed(self, ticket: Ticket) -> tuple[bool, str]:
        """Return whether the Ticket may end after its worker report."""
        if self.by_ticket(ticket.id) is None:
            return False, "flowMissing"
        return self.delivery.may_finish(ticket)


def waiting_after_cap(ticket_id: str, assignment_id: str, at: str) -> Report:
    """Build the WAITING report used when a worker hits its continuation cap."""
    del ticket_id
    return Report(
        assignment_id=assignment_id,
        kind=ReportKind.WAITING,
        summary="The worker reached its continuation cap.",
        at=at,
    )


def wake_idle_ticket(engine: object, ticket: Ticket, trigger: Trigger) -> bool:
    """Wake an idle Ticket with a fresh Assignment from an inbound Trigger.

    An idle Ticket is a settled worker with no running Assignment.  A new
    Trigger (CI result, comment or person answer) must start a new goal-bounded
    worker run rather than trying to resume the finished one.
    """
    if ticket_stage(ticket) != TicketStage.IDLE:
        return False
    assignment_id = key("assignment", ticket.id, str(len(ticket.assignments) + 1))
    with engine.repo.atomic:  # type: ignore[attr-defined]
        ticket.inputs.append(
            Input(
                id=key(trigger.id, ticket.id, "wake"),
                ticket_id=ticket.id,
                kind="wake",
                trigger_id=trigger.id,
                text=trigger.text,
                human=trigger.human,
                at=trigger.at,
            )
        )
        ticket.assignments.append(Assignment(id=assignment_id, ticket_id=ticket.id))
        ticket.latest_report = None
        engine.repo.tickets.save(ticket)  # type: ignore[attr-defined]
        engine.effects.enqueue(  # type: ignore[attr-defined]
            "launch",
            ticket.id,
            {"ticket_id": ticket.id, "assignment_id": assignment_id},
        )
    return True


__all__ = ["FlowBinding", "FlowMap", "wake_idle_ticket", "waiting_after_cap"]
