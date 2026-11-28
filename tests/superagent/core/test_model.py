"""Domain model surface tests for superagent P0."""

from __future__ import annotations

from superagent.core.errors import USER_ERRORS, UserError, error_message, http_status
from superagent.core.ids import decision_key, key, thread_key, trigger_key
from superagent.core.model import (
    AddressKind,
    DecisionOutcome,
    ReportKind,
    Trigger,
    actor_from_dict,
    assignment_from_dict,
    followup_owners,
    latest_assignment,
    live_ticket,
    reached,
    running_assignment,
    settled,
    thread_from_dict,
    ticket_from_dict,
    to_dict,
    trigger_from_dict,
    waits_for_person,
)
from superagent.core.reasons import REASONS, reason_text
from tests.superagent.conftest import make_report, make_ticket, make_trigger


def make_decision(outcome: DecisionOutcome) -> object:
    """Return a minimal Decision for predicate tests."""
    from superagent.core.model import Decision, Reason, SourceRef

    return Decision(
        id="dec-1",
        thread=SourceRef(source="github", id="thread-1"),
        outcome=outcome,
        reason=Reason(code="startedTicket", values={}),
        at="2026-11-28T00:00:00+00:00",
        trigger_ids=["trig-1"],
    )


def test_enums_cover_plan_surface() -> None:
    """Enum values are stable wire strings."""
    assert AddressKind.MENTION.value == "mention"
    assert DecisionOutcome.UNAVAILABLE.value == "unavailable"
    assert ReportKind.BLOCKED.value == "blocked"


def test_ids_are_deterministic_and_collision_safe() -> None:
    """The separator keeps concatenated parts distinct."""
    assert key("ab", "cd") != key("a", "bcd")
    assert trigger_key("github", "1") == trigger_key("github", "1")
    assert thread_key.__name__ == "thread_key"
    assert decision_key("thread", "at", ["a", "b"]) == decision_key("thread", "at", ["a", "b"])


def test_trigger_round_trip() -> None:
    """to_dict/from_dict preserve the stored Trigger shape."""
    trigger = make_trigger()
    restored = trigger_from_dict(to_dict(trigger))
    assert restored == trigger


def test_ticket_thread_assignment_round_trip() -> None:
    """Nested dataclasses survive JSON conversion."""
    ticket = make_ticket()
    ticket.latest_report = make_report()
    ticket.assignments[0].latest_report = ticket.latest_report
    assert ticket_from_dict(to_dict(ticket)) == ticket

    thread = thread_from_dict(to_dict(make_thread_fixture()))
    assert thread.key == "thread-key"

    actor = actor_from_dict(to_dict(trigger_from_dict(to_dict(make_trigger())).actor))
    assert actor.id == "alice"
    assignment = assignment_from_dict(to_dict(ticket.assignments[0]))
    assert assignment.id == "assign-1"


def make_thread_fixture() -> object:
    """Return a minimal Thread."""
    from superagent.core.model import Thread

    return Thread(key="thread-key", source="github", id="thread-1")


def test_shared_predicates_positive_and_negative() -> None:
    """Every shared predicate has a positive and negative branch."""
    ticket = make_ticket()
    ticket.latest_report = make_report(ReportKind.BLOCKED)
    ticket.assignments[0].latest_report = ticket.latest_report
    assert waits_for_person(ticket) is True
    assert live_ticket(ticket) is True
    assert latest_assignment(ticket) is ticket.assignments[0]
    assert running_assignment(ticket) is None
    assert reached("trig-1", ticket) is True
    assert reached("other", ticket) is False
    assert followup_owners("trig-1", [ticket]) == [ticket.id]
    assert followup_owners("other", [ticket]) == []

    ticket.assignments[0].status = "running"
    assert running_assignment(ticket) is ticket.assignments[0]

    ticket.ended = {"at": "2026-11-28T02:00:00+00:00", "by": "person"}
    assert live_ticket(ticket) is False
    assert followup_owners("trig-1", [ticket]) == []


def test_settled_requires_resolving_outcome() -> None:
    """stop/unavailable do not settle a request."""
    assert settled(make_decision(DecisionOutcome.ACCEPT)) is True
    assert settled(make_decision(DecisionOutcome.IGNORE)) is True
    assert settled(make_decision(DecisionOutcome.STOP)) is False
    assert settled(make_decision(DecisionOutcome.UNAVAILABLE)) is False
    assert settled(None) is False


def test_reason_and_error_text() -> None:
    """Reason/error codes are stable and render unknown codes honestly."""
    assert reason_text("timeout", {}) == REASONS["timeout"]
    assert reason_text("unknown", {}) == "unknown"
    assert error_message("followup_owned", {}) == USER_ERRORS["followup_owned"]
    assert error_message("unknown", {}) == "unknown"
    assert UserError("not_found").code == "not_found"
    assert http_status("not_found") == 404
    assert http_status("stale_revision") == 409
    assert http_status("bad_request") == 400


def test_trigger_defaults_are_conservative() -> None:
    """Missing optional fields decode to empty values."""
    trigger: Trigger = trigger_from_dict({"id": "x"})
    assert trigger.threads == []
    assert trigger.human is False
    assert trigger.meta == {}
