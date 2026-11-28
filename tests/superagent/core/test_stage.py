"""Ticket stage table tests."""

from __future__ import annotations

from superagent.core.model import AssignmentStatus, ReportKind
from superagent.core.stage import TicketStage, can_retry, ticket_stage
from tests.superagent.conftest import make_report, make_ticket


def test_ended_ticket_has_no_stage() -> None:
    """Ended Tickets are outside the stage table."""
    ticket = make_ticket()
    ticket.ended = {"at": "2026-11-28T02:00:00+00:00", "by": "person"}
    assert ticket_stage(ticket) is None
    assert can_retry(ticket) is False


def test_person_delivery_has_priority() -> None:
    """A failed Input delivery needs a person first."""
    ticket = make_ticket()
    ticket.latest_report = make_report(ReportKind.READY)
    ticket.inputs[0:0] = []
    from superagent.core.model import Input

    ticket.inputs.append(
        Input(
            id="input-1",
            ticket_id=ticket.id,
            kind="fanout",
            trigger_id="trig-1",
            text="hello",
            last_error="gone",
        )
    )
    assert ticket_stage(ticket) == TicketStage.PERSON_DELIVERY
    assert can_retry(ticket) is True


def test_person_answer_and_review() -> None:
    """Attention and ready reports are shown to a person."""
    ticket = make_ticket()
    ticket.latest_report = make_report(ReportKind.ATTENTION, "2026-11-28T01:00:00+00:00")
    ticket.latest_report.question = "Which branch?"
    assert ticket_stage(ticket) == TicketStage.PERSON_ANSWER
    assert can_retry(ticket) is False

    ticket.latest_report = make_report(ReportKind.READY, "2026-11-28T01:01:00+00:00")
    assert ticket_stage(ticket) == TicketStage.PERSON_REVIEW


def test_person_blocked_failed_is_retryable() -> None:
    """Blocked and failed reports wait for a person and can be retried."""
    for kind in (ReportKind.BLOCKED, ReportKind.FAILED):
        ticket = make_ticket()
        ticket.latest_report = make_report(kind)
        assert ticket_stage(ticket) == TicketStage.PERSON_BLOCKED_FAILED
        assert can_retry(ticket) is True


def test_queued_running_and_idle() -> None:
    """The three working-stage rows are mutually exclusive."""
    ticket = make_ticket()
    ticket.assignments[0].status = AssignmentStatus.ENDED
    assert ticket_stage(ticket) == TicketStage.IDLE

    ticket.assignments[0].status = AssignmentStatus.PENDING
    assert ticket_stage(ticket) == TicketStage.WORKING_QUEUED

    ticket.assignments[0].status = AssignmentStatus.RUNNING
    assert ticket_stage(ticket) == TicketStage.WORKING

    ticket.assignments[0].status = AssignmentStatus.ENDED
    from superagent.core.model import Input

    ticket.inputs.append(
        Input(
            id="input-1",
            ticket_id=ticket.id,
            kind="fanout",
            trigger_id="trig-1",
            text="hello",
        )
    )
    assert ticket_stage(ticket) == TicketStage.WORKING_QUEUED
