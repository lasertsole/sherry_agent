"""Engine domain-rule tests on memory repositories."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, UTC

from superagent.core.model import AddressKind, AssignmentStatus, Channel, Report, ReportKind, Reason
from superagent.core.ports import DomainEvents
from tests.superagent.conftest import make_trigger, make_engine


def accepted_engine():
    engine, ctx = make_engine()
    trigger = replace(make_trigger(), addressed=AddressKind.MENTION)
    assert engine.receive(trigger) == "stored"
    assert engine.receive(trigger) == "duplicate"
    engine.evaluate_due()
    thread = ctx["repo"].threads.get(engine.thread_key(trigger.threads[0]))
    assert thread is not None
    assert thread.eval is not None
    card = engine.candidate(trigger.id)
    assert card is not None
    engine.candidate_delivered(card["eval_id"])
    engine.decide(trigger.id, "accept", Reason("startedTicket", {}))
    ticket = ctx["repo"].tickets.list()[0]
    return engine, ctx, trigger, ticket


def test_receive_duplicate_filtered_and_stored() -> None:
    engine, ctx = make_engine()
    trigger = make_trigger()
    assert engine.receive(trigger) == "stored"
    assert engine.receive(trigger) == "duplicate"
    filtered = replace(make_trigger("trig-filtered"), meta={"filtered": True})
    assert engine.receive(filtered) == "filtered"
    assert len(ctx["repo"].triggers.list()) == 1


def test_accept_creates_ticket_mandate_ack_and_launch() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    assert ticket.mandate.policy_version
    assert ticket.mandate.strength.value == "explicit"
    assert ticket.origin["trigger_id"] == trigger.id
    assert ctx["effects"].count("launch") == 1
    assert ctx["repo"].outbox.list()[0].purpose == Channel.ACKNOWLEDGEMENT
    assert any(event.name == DomainEvents.TICKET_CREATED for event in ctx["events"])


def test_fanout_does_not_queue_a_second_eval() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    follow = replace(make_trigger("trig-2"), threads=trigger.threads)
    assert engine.receive(follow) == "stored"
    thread = ctx["repo"].threads.get(engine.thread_key(trigger.threads[0]))
    assert thread is not None
    assert thread.eval is None
    assert thread.queued_trigger_ids == []
    saved = ctx["repo"].tickets.get(ticket.id)
    assert saved is not None
    assert any(item.trigger_id == "trig-2" for item in saved.inputs)
    assert ctx["effects"].count("delivery") >= 1


def test_assignment_report_ready_ends_ticket() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    assignment = ticket.assignments[0]
    engine.assignment_started(ticket.id, assignment.id)
    saved = ctx["repo"].tickets.get(ticket.id)
    assert saved is not None
    assert saved.assignments[0].status == AssignmentStatus.RUNNING
    report = Report(
        assignment_id=assignment.id,
        kind=ReportKind.READY,
        summary="done",
        at=engine.now(),
        pull_request={"number": 1},
    )
    engine.report(ticket.id, report)
    ended = ctx["repo"].tickets.get(ticket.id)
    assert ended is not None
    assert ended.ended is not None
    assert ended.latest_report is not None
    assert ended.latest_report.kind == ReportKind.READY
    assert ctx["effects"].count("relay") >= 1


def test_stop_and_retry_rules() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    engine.stop_ticket(ticket.id, Reason("stoppedTicket", {}))
    stopped = ctx["repo"].tickets.get(ticket.id)
    assert stopped is not None and stopped.ended is not None
    assert stopped.ended["by"] == "person"
    assert ticket.id in ctx["executor"].stopped

    engine, ctx, trigger, ticket = accepted_engine()
    report = Report(
        assignment_id=ticket.assignments[0].id,
        kind=ReportKind.BLOCKED,
        summary="need input",
        at=engine.now(),
    )
    engine.report(ticket.id, report)
    engine.retry_ticket(ticket.id)
    retried = ctx["repo"].tickets.get(ticket.id)
    assert retried is not None and len(retried.assignments) == 2
    assert retried.latest_report is None


def test_inferred_observe_does_not_accept() -> None:
    engine, ctx = make_engine()
    trigger = make_trigger()
    engine.receive(trigger)
    engine.evaluate_due()
    card = engine.candidate(trigger.id)
    assert card is not None
    engine.candidate_delivered(card["eval_id"])
    engine.decide(trigger.id, "accept", Reason("ignoredFromCard", {}))
    assert ctx["repo"].tickets.list() == []
    decisions = ctx["repo"].decisions.list()
    assert len(decisions) == 1
    assert decisions[0].outcome.value == "ignore"
    assert decisions[0].reason.values.get("would_accept") is True


def test_timeout_requeues_then_marks_unavailable() -> None:
    engine, ctx = make_engine()
    trigger = make_trigger()
    thread_key = engine.thread_key(trigger.threads[0])
    engine.receive(trigger)

    for cycle in range(3):
        engine.evaluate_due()
        thread = ctx["repo"].threads.get(thread_key)
        assert thread is not None and thread.eval is not None
        card = engine.candidate(trigger.id)
        assert card is not None
        engine.candidate_delivered(card["eval_id"])
        thread = ctx["repo"].threads.get(thread_key)
        assert thread is not None and thread.eval is not None
        thread.eval.deadline = datetime.now(UTC) - timedelta(seconds=1)
        ctx["repo"].threads.save(thread)
        engine.evaluate_due()
        if cycle < 2:
            thread = ctx["repo"].threads.get(thread_key)
            assert thread is not None and thread.expired_count == cycle + 1
            assert trigger.id in thread.queued_trigger_ids

    outcomes = [item.outcome.value for item in ctx["repo"].decisions.list()]
    assert "unavailable" in outcomes


def test_policy_changed_requeues_eval_and_notices_tickets() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    from superagent.core.model import SourceRef

    other_thread = SourceRef(source="github", id="thread-policy")
    other = replace(
        make_trigger("trig-policy"),
        threads=[other_thread],
        addressed=AddressKind.MENTION,
    )
    engine.receive(other)
    engine.evaluate_due()
    thread = ctx["repo"].threads.get(engine.thread_key(other_thread))
    assert thread is not None and thread.eval is not None
    old = engine.policy_get()
    ctx["settings"]["rules"]["rule-1"]["keywords"] = ["changed"]
    engine.policy_changed(old, engine.policy_get())
    thread = ctx["repo"].threads.get(engine.thread_key(other_thread))
    assert thread is not None and thread.eval is None
    assert "trig-policy" in thread.queued_trigger_ids
    assert ctx["effects"].count("notice") >= 1
