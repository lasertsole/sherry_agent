"""Executor facts: Assignments, Inputs and Reports (prototype ``core/assignments.py``)."""

from __future__ import annotations

from typing import Any

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import (
    Assignment,
    AssignmentStatus,
    Input,
    Report,
    ReportKind,
    TicketOutcome,
)
from superagent.core.part import EnginePart
from superagent.core.ports import DomainEvent, DomainEvents


class Assignments(EnginePart):
    """Records worker facts without owning authorization decisions."""

    def _ticket(self, ticket_id: str) -> Any:
        ticket = self.engine.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        return ticket

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        """Mark an Assignment running and deliver waiting Inputs."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        with engine.repo.atomic as tx:
            for assignment in ticket.assignments:
                if assignment.id == assignment_id:
                    assignment.status = AssignmentStatus.RUNNING
                    assignment.started_at = engine.now()
            waiting = [item.id for item in ticket.inputs if not item.delivered]
            engine.repo.tickets.save(ticket)
            if waiting:
                engine.effects.enqueue(
                    "delivery",
                    ticket_id,
                    {"ticket_id": ticket_id, "assignment_id": assignment_id, "input_ids": waiting},
                )
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(
                        DomainEvents.ASSIGNMENT_STARTED,
                        {"ticket_id": ticket_id, "assignment_id": assignment_id},
                    )
                )
            )

    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        """Mark Inputs delivered to a running Assignment."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        with engine.repo.atomic:
            for item in ticket.inputs:
                if item.id in input_ids:
                    item.delivered = True
            engine.repo.tickets.save(ticket)

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        """Mark Inputs as taken into the worker history."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        with engine.repo.atomic:
            for item in ticket.inputs:
                if item.id in input_ids:
                    item.read = True
            engine.repo.tickets.save(ticket)

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        """Report waiting when a turn ends with no background work."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        with engine.repo.atomic:
            report = Report(
                assignment_id=assignment_id,
                kind=ReportKind.WAITING,
                summary="The worker turn ended.",
                at=engine.now(),
            )
            for assignment in ticket.assignments:
                if assignment.id == assignment_id:
                    assignment.latest_report = report
            ticket.latest_report = report
            engine.repo.tickets.save(ticket)
            engine.effects.enqueue(
                "relay",
                ticket_id,
                {"ticket_id": ticket_id, "report": report.at, "kind": "waiting"},
            )

    def delivery_failed(
        self,
        ticket_id: str,
        assignment_id: str,
        error: str,
        input_id: str | None = None,
    ) -> None:
        """Record a delivery failure and retry via the delivery Effect."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        with engine.repo.atomic:
            for item in ticket.inputs:
                if input_id is None or item.id == input_id:
                    item.last_error = error
                    item.attempts += 1
            engine.repo.tickets.save(ticket)
            engine.effects.enqueue(
                "delivery",
                ticket_id,
                {"ticket_id": ticket_id, "assignment_id": assignment_id, "error": error},
            )

    def report(self, ticket_id: str, report: Report) -> None:
        """Record a worker Report and apply terminal outcomes."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        with engine.repo.atomic as tx:
            for assignment in ticket.assignments:
                if assignment.id == report.assignment_id:
                    assignment.latest_report = report
                    if report.kind in (ReportKind.READY, ReportKind.DROPPED):
                        assignment.status = AssignmentStatus.ENDED
                        assignment.ended_at = report.at
            ticket.latest_report = report
            ticket.last_said = report.summary
            engine.repo.tickets.save(ticket)
            if report.kind in (ReportKind.READY, ReportKind.BLOCKED, ReportKind.FAILED):
                engine.effects.enqueue(
                    "relay",
                    ticket_id,
                    {"ticket_id": ticket_id, "report": report.at, "kind": report.kind.value},
                )
            if report.kind == ReportKind.DROPPED:
                engine.tickets.end(ticket, TicketOutcome.DROPPED, report.at)
            elif report.kind == ReportKind.READY and not any(item.own for item in ticket.follows):
                engine.tickets.end(ticket, TicketOutcome.COMPLETED, report.at)
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(
                        DomainEvents.ASSIGNMENT_ENDED,
                        {"ticket_id": ticket_id, "kind": report.kind.value},
                    )
                )
            )

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        """Restart a running Assignment after a host restart."""
        engine = self.engine
        ticket = self._ticket(ticket_id)
        new_id = key("assignment", ticket_id, str(len(ticket.assignments) + 1))
        with engine.repo.atomic:
            for assignment in ticket.assignments:
                if assignment.id == assignment_id:
                    assignment.status = AssignmentStatus.ENDED
                    assignment.ended_at = engine.now()
            recovery = Input(
                id=key(assignment_id, "recovery"),
                ticket_id=ticket_id,
                kind="resume",
                trigger_id=None,
                text="The previous assignment was interrupted; continue from the latest state.",
                at=engine.now(),
            )
            ticket.inputs.append(recovery)
            ticket.assignments.append(Assignment(id=new_id, ticket_id=ticket_id))
            engine.repo.tickets.save(ticket)
            engine.effects.enqueue(
                "launch",
                ticket_id,
                {
                    "ticket_id": ticket_id,
                    "assignment_id": new_id,
                    "recovery_note": recovery.text,
                },
            )


__all__ = ["Assignments"]
