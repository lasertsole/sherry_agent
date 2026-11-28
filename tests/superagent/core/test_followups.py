"""Follow-up ownership, attach and handback tests."""

from __future__ import annotations

from dataclasses import replace

from superagent.core.errors import UserError
from superagent.core.model import AddressKind, Input, Reason, SourceRef, TicketOutcome
from superagent.core.model import followup_owners, reached
from tests.superagent.conftest import make_engine, make_trigger


def accepted_engine():
    engine, ctx = make_engine()
    trigger = replace(make_trigger(), addressed=AddressKind.MENTION)
    engine.receive(trigger)
    engine.evaluate_due()
    card = engine.candidate(trigger.id)
    engine.candidate_delivered(card["eval_id"])
    engine.decide(trigger.id, "accept", Reason("startedTicket", {}))
    ticket = ctx["repo"].tickets.list()[0]
    return engine, ctx, trigger, ticket


def test_reached_and_followup_owners() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    assert reached(trigger.id, ticket) is True
    assert reached("unknown", ticket) is False
    assert followup_owners(trigger.id, [ticket]) == [ticket.id]


def test_attach_requires_explicit_pointer() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    vague = replace(make_trigger("trig-vague"), threads=trigger.threads)
    engine.receive(vague)
    try:
        engine.attach(ticket.id, vague.id, "related?")
    except UserError:
        pass
    else:
        raise AssertionError("non-explicit attach must be rejected")

    explicit = replace(
        make_trigger("trig-explicit"),
        threads=trigger.threads,
        meta={"ticket_id": ticket.id},
    )
    engine.receive(explicit)
    engine.attach(ticket.id, explicit.id, "explicit")
    saved = ctx["repo"].tickets.get(ticket.id)
    assert saved is not None
    assert any(item.trigger_id == explicit.id for item in saved.inputs)


def test_handback_requeues_the_trigger() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    explicit = replace(
        make_trigger("trig-handback"),
        threads=trigger.threads,
        meta={"ticket_id": ticket.id},
    )
    engine.receive(explicit)
    engine.attach(ticket.id, explicit.id, "hand back")
    assert engine.handback(ticket.id, explicit.id, Reason("routed", {})) is True
    thread = ctx["repo"].threads.get(engine.thread_key(trigger.threads[0]))
    assert thread is not None
    assert "trig-handback" in thread.queued_trigger_ids


def test_end_releases_unread_followups() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    ticket.inputs.append(
        Input(
            id="unread-1",
            ticket_id=ticket.id,
            kind="fanout",
            trigger_id="trig-release",
            text="please answer",
        )
    )
    engine.receive(
        replace(
            make_trigger("trig-release"), threads=trigger.threads, meta={"ticket_id": ticket.id}
        )
    )
    ctx["repo"].tickets.save(ticket)
    engine.tickets.end(ticket, TicketOutcome.COMPLETED, engine.now())
    thread = ctx["repo"].threads.get(engine.thread_key(trigger.threads[0]))
    assert thread is not None
    assert "trig-release" in thread.queued_trigger_ids


def test_context_follow_does_not_grant_ownership() -> None:
    engine, ctx, trigger, ticket = accepted_engine()
    other = SourceRef(source="github", id="thread-other")
    engine.follow_for_context(ticket.id, other)
    saved = ctx["repo"].tickets.get(ticket.id)
    assert saved is not None
    assert saved.follows[-1].thread == other
    assert reached(trigger.id, ticket) is True
