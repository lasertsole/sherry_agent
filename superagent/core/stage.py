"""Ticket stage table (prototype ``src/core/stage.py``)."""

from __future__ import annotations

from enum import StrEnum

from superagent.core.model import AssignmentStatus, ReportKind, Ticket, running_assignment


class TicketStage(StrEnum):
    """Stages shown in the panel, in table priority order."""

    PERSON_DELIVERY = "person/delivery"
    PERSON_ANSWER = "person/answer"
    PERSON_REVIEW = "person/review"
    PERSON_BLOCKED_FAILED = "person/blocked,failed"
    WORKING_QUEUED = "working/queued"
    IDLE = "idle"
    WORKING = "working"


def ticket_stage(ticket: Ticket, now: str | None = None) -> TicketStage | None:
    """Return the first matching stage; ``now`` is reserved for future SLA rows."""
    del now
    if ticket.ended is not None:
        return None

    if any(inp.last_error and not inp.delivered for inp in ticket.inputs):
        return TicketStage.PERSON_DELIVERY

    report = ticket.latest_report
    if report is not None and report.kind == ReportKind.ATTENTION:
        return TicketStage.PERSON_ANSWER
    if report is not None and report.kind == ReportKind.READY:
        return TicketStage.PERSON_REVIEW
    if report is not None and report.kind in (ReportKind.BLOCKED, ReportKind.FAILED):
        return TicketStage.PERSON_BLOCKED_FAILED

    if any(not inp.delivered for inp in ticket.inputs):
        return TicketStage.WORKING_QUEUED
    assignment = running_assignment(ticket)
    if assignment is not None:
        return TicketStage.WORKING
    if ticket.assignments and ticket.assignments[-1].status == AssignmentStatus.PENDING:
        return TicketStage.WORKING_QUEUED
    return TicketStage.IDLE


def can_retry(ticket: Ticket) -> bool:
    """Return whether a person may retry this Ticket."""
    return ticket_stage(ticket) in (
        TicketStage.PERSON_DELIVERY,
        TicketStage.PERSON_BLOCKED_FAILED,
    )
