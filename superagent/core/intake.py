"""Trigger intake, fan-out and Eval queueing (prototype ``src/core/intake.py``)."""

from __future__ import annotations

from typing import Any

from superagent.core.ids import key
from superagent.core.model import Input, SourceRef, Thread, Trigger, to_dict
from superagent.core.part import EnginePart
from superagent.core.ports import DomainEvent, DomainEvents


class Intake(EnginePart):
    """Receives Triggers and fans them out to subscribed Tickets."""

    def receive(self, trigger: Trigger) -> str:
        """Store a Trigger, fan it out and queue it for assessment."""
        engine = self.engine
        repo = engine.repo
        if trigger.meta.get("filtered"):
            return "filtered"
        with repo.atomic as tx:
            if repo.triggers.exists(trigger.id):
                return "duplicate"
            repo.triggers.save(trigger)
            primary = (
                trigger.threads[0] if trigger.threads else SourceRef(trigger.source, "default")
            )
            thread_key = engine.thread_key(primary)
            thread = repo.threads.get(thread_key)
            if thread is None:
                thread = Thread(key=thread_key, source=primary.source, id=primary.id)
            for ref in trigger.threads:
                repo.trigger_threads.save(trigger.id, ref, trigger.at, trigger.human)

            delivered = False
            for ticket in repo.tickets.list():
                if ticket.ended is not None:
                    continue
                if not any(
                    sub.thread.source == primary.source and sub.thread.id == primary.id
                    for sub in ticket.follows
                ):
                    continue
                item = Input(
                    id=key(trigger.id, ticket.id, "input"),
                    ticket_id=ticket.id,
                    kind="fanout",
                    trigger_id=trigger.id,
                    text=trigger.text,
                    human=trigger.human,
                    at=trigger.at,
                )
                ticket.inputs.append(item)
                repo.tickets.save(ticket)
                delivered = True
                engine.effects.enqueue(
                    "delivery",
                    ticket.id,
                    {"ticket_id": ticket.id, "input_ids": [item.id]},
                )

            if not delivered and trigger.id not in thread.queued_trigger_ids:
                thread.queued_trigger_ids.append(trigger.id)
            repo.threads.save(thread)
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(
                        DomainEvents.TRIGGER_RECEIVED,
                        {
                            "trigger_id": trigger.id,
                            "delivered": delivered,
                            "trigger": to_dict(trigger),
                        },
                    )
                )
            )
        return "stored"

    def bind_thread(self, ticket_id: str, thread: Any, own: bool) -> None:
        """Subscribe a Ticket to a Thread."""
        engine = self.engine
        repo = engine.repo
        ticket = repo.tickets.get(ticket_id)
        if ticket is None:
            return
        with repo.atomic as tx:
            if any(
                sub.thread.source == thread.source and sub.thread.id == thread.id
                for sub in ticket.follows
            ):
                return
            from superagent.core.model import Follow

            ticket.follows.append(Follow(thread=thread, own=own))
            repo.tickets.save(ticket)
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(
                        DomainEvents.THREAD_SUBSCRIBED,
                        {"ticket_id": ticket_id, "thread": to_dict(thread)},
                    )
                )
            )

    def thread_closed(self, thread: Any, outcome: str, at: str) -> None:
        """Record a closed followed Thread and end Tickets whose own PRs closed."""
        engine = self.engine
        repo = engine.repo
        with repo.atomic:
            for ticket in repo.tickets.list():
                changed = False
                for follow in ticket.follows:
                    if follow.thread.source == thread.source and follow.thread.id == thread.id:
                        follow.closed = {"at": at, "outcome": outcome}
                        changed = True
                if not changed:
                    continue
                repo.tickets.save(ticket)
                own_follows = [item for item in ticket.follows if item.own]
                if own_follows and all(item.closed for item in own_follows):
                    engine.tickets.end(ticket, outcome, at)


__all__ = ["Intake"]
