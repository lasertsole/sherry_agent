"""Ticket lifecycle, follow-ups and ownership (prototype ``src/core/tickets.py``)."""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import (
    Assignment,
    AssignmentStatus,
    Channel,
    Follow,
    Input,
    Mandate,
    MandateBasis,
    Receipt,
    Strength,
    Ticket,
    TicketOutcome,
    to_dict,
)
from superagent.core.part import EnginePart
from superagent.core.ports import DomainEvent, DomainEvents
from superagent.core.stage import can_retry
from superagent.limits import MAX_ACK_TEXT


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Tickets(EnginePart):
    """Owns Ticket creation, resume, retry, stop, end and Thread follows."""

    def create(
        self,
        trigger: Any,
        thread: Any,
        gate: dict[str, Any],
        reason: Any,
    ) -> Ticket:
        """Create a Ticket from an accepted Eval."""
        engine = self.engine
        repo = engine.repo
        policy = engine.policy_get()
        ticket_id = key("ticket", trigger.id)
        assignment_id = key("assignment", ticket_id, "1")
        prepared = engine.prepare_ticket(trigger, gate)
        strength_raw = gate.get("strength", Strength.EXPLICIT.value)
        basis_raw = gate.get("basis")
        try:
            strength = Strength(strength_raw)
        except ValueError:
            strength = Strength.EXPLICIT
        try:
            basis = MandateBasis(basis_raw) if basis_raw else MandateBasis.ADMISSION
        except ValueError:
            basis = MandateBasis.ADMISSION
        mandate = Mandate(
            policy_version=str(gate.get("policy_version") or policy.version),
            scope=str(gate.get("scope") or trigger.source),
            strength=strength,
            matched_rules=list(gate.get("matched_rules") or []),
            sender=trigger.actor,
            via=basis,
            basis_detail={
                "scope": gate.get("scope") or trigger.source,
                "cell": gate.get("cell"),
                "sender": to_dict(trigger.actor),
                "rules": list(gate.get("matched_rules") or []),
            },
        )
        channels = [trigger.channel] if trigger.channel else []
        ticket = Ticket(
            id=ticket_id,
            origin={
                "trigger_id": trigger.id,
                "trigger_ids": list(gate.get("trigger_ids") or [trigger.id]),
            },
            mandate=mandate,
            execution=dict(prepared),
            channels=channels,
            inputs=[],
            follows=[],
            assignments=[Assignment(id=assignment_id, ticket_id=ticket_id)],
            latest_report=None,
        )
        with repo.atomic as tx:
            repo.tickets.save(ticket)
            primary = trigger.threads[0] if trigger.threads else None
            if primary is not None:
                engine.intake.bind_thread(ticket_id, primary, own=False)
            if not prepared.get("silence_ack") and channels:
                ack = Receipt(
                    id=key("receipt", ticket_id, "ack"),
                    ticket_id=ticket_id,
                    channel=channels[0],
                    purpose=Channel.ACKNOWLEDGEMENT,
                    text=str(reason.code if hasattr(reason, "code") else "Accepted")[:MAX_ACK_TEXT],
                )
                engine.outbox.send(ack)
            engine.effects.enqueue(
                "launch",
                ticket_id,
                {"ticket_id": ticket_id, "assignment_id": assignment_id},
            )
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(DomainEvents.TICKET_CREATED, {"ticket_id": ticket_id})
                )
            )
        return ticket

    def _matching(self, thread: Any) -> list[Ticket]:
        return [
            ticket
            for ticket in self.engine.repo.tickets.list_open()
            if any(
                follow.thread.source == thread.source and follow.thread.id == thread.id
                for follow in ticket.follows
            )
        ]

    def stop_matching(self, thread: Any, reason: Any) -> None:
        """Permanently stop Tickets that own this Thread."""
        for ticket in self._matching(thread):
            self.stop(ticket, reason)

    def stop(self, ticket: Ticket, reason: Any = None) -> None:
        """Revoke a Mandate and permanently end a Ticket."""
        engine = self.engine
        repo = engine.repo
        with repo.atomic as tx:
            ticket.ended = {
                "at": engine.now(),
                "by": "person",
                "why": "stop",
                "reason": to_dict(reason) if reason is not None else None,
            }
            repo.tickets.save(ticket)
            engine.executor.stop(ticket.id)
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(DomainEvents.TICKET_ENDED, {"ticket_id": ticket.id, "why": "stop"})
                )
            )

    def end(self, ticket: Ticket, outcome: Any, at: str | None = None) -> None:
        """End a Ticket with a terminal outcome and release follow-ups."""
        engine = self.engine
        repo = engine.repo
        value = outcome.value if isinstance(outcome, TicketOutcome) else str(outcome)
        ended_at = at or engine.now()
        with repo.atomic as tx:
            ticket.ended = {"at": ended_at, "by": "system", "why": value, "outcome": value}
            for assignment in ticket.assignments:
                if assignment.status == AssignmentStatus.RUNNING:
                    assignment.status = AssignmentStatus.ENDED
                    assignment.ended_at = ended_at
            repo.tickets.save(ticket)
            engine.executor.stop(ticket.id)
            self._release_followups(ticket)
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(
                        DomainEvents.TICKET_ENDED, {"ticket_id": ticket.id, "outcome": value}
                    )
                )
            )

    def _release_followups(self, ticket: Ticket) -> None:
        for item in ticket.inputs:
            if item.delivered and item.read:
                continue
            try:
                self.engine.evals.reevaluate(item.trigger_id) if item.trigger_id else None
            except UserError:
                continue

    def resume(self, ticket: Ticket, trigger: Any) -> bool:
        """Reopen an ended Ticket after a person's message passes revalidation."""
        engine = self.engine
        repo = engine.repo
        policy = engine.policy_get()
        result = policy.revalidate(ticket.mandate)
        if not result.ok:
            return False
        assignment_id = key("assignment", ticket.id, str(len(ticket.assignments) + 1))
        item = Input(
            id=key(trigger.id, ticket.id, "resume"),
            ticket_id=ticket.id,
            kind="resume",
            trigger_id=trigger.id,
            text=trigger.text,
            human=trigger.human,
            at=trigger.at,
        )
        with repo.atomic as tx:
            ticket.ended = None
            ticket.inputs.append(item)
            ticket.assignments.append(Assignment(id=assignment_id, ticket_id=ticket.id))
            repo.tickets.save(ticket)
            engine.effects.enqueue(
                "launch",
                ticket.id,
                {"ticket_id": ticket.id, "assignment_id": assignment_id},
            )
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(DomainEvents.TICKET_RESUMED, {"ticket_id": ticket.id})
                )
            )
        return True

    def retry(self, ticket: Ticket) -> None:
        """End the current Assignment and start a fresh one."""
        if not can_retry(ticket):
            raise UserError("cannot_retry", {"ticket_id": ticket.id})
        engine = self.engine
        repo = engine.repo
        assignment_id = key("assignment", ticket.id, str(len(ticket.assignments) + 1))
        with repo.atomic as tx:
            for assignment in ticket.assignments:
                if assignment.status == AssignmentStatus.RUNNING:
                    assignment.status = AssignmentStatus.ENDED
                    assignment.ended_at = engine.now()
            ticket.assignments.append(Assignment(id=assignment_id, ticket_id=ticket.id))
            ticket.latest_report = None
            repo.tickets.save(ticket)
            engine.effects.enqueue(
                "launch",
                ticket.id,
                {"ticket_id": ticket.id, "assignment_id": assignment_id},
            )
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(
                        DomainEvents.ASSIGNMENT_STARTED,
                        {"ticket_id": ticket.id, "assignment_id": assignment_id},
                    )
                )
            )

    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> None:
        """Attach a Trigger only when it explicitly points at the Ticket."""
        engine = self.engine
        repo = engine.repo
        ticket = repo.tickets.get(ticket_id)
        trigger = repo.triggers.get(trigger_id)
        if ticket is None or trigger is None:
            raise UserError("not_found", {"ticket_id": ticket_id, "trigger_id": trigger_id})
        explicit = (
            trigger.meta.get("ticket_id") == ticket_id
            or trigger.meta.get("attach_ticket_id") == ticket_id
            or ticket_id in trigger.text
        )
        if not explicit:
            raise UserError("invalid_value", {"ticket_id": ticket_id})
        with repo.atomic:
            ticket.inputs.append(
                Input(
                    id=key(trigger_id, ticket_id, "attach"),
                    ticket_id=ticket_id,
                    kind="text",
                    trigger_id=trigger_id,
                    text=note or trigger.text,
                    human=trigger.human,
                    at=trigger.at,
                )
            )
            repo.tickets.save(ticket)
            if trigger.threads:
                engine.intake.bind_thread(ticket_id, trigger.threads[0], own=False)

    def unsubscribe(self, thread: Any) -> None:
        """Remove all subscriptions for a Thread."""
        engine = self.engine
        repo = engine.repo
        with repo.atomic as tx:
            for ticket in repo.tickets.list():
                before = len(ticket.follows)
                ticket.follows = [
                    item
                    for item in ticket.follows
                    if not (item.thread.source == thread.source and item.thread.id == thread.id)
                ]
                if len(ticket.follows) == before:
                    continue
                repo.tickets.save(ticket)
                tx.after_commit(
                    lambda tid=ticket.id: engine.emit(
                        DomainEvent(
                            DomainEvents.THREAD_UNSUBSCRIBED,
                            {"ticket_id": tid, "thread": to_dict(thread)},
                        )
                    )
                )

    def follow_for_context(self, ticket_id: str, thread: Any) -> None:
        """Follow a Thread for context without granting authorization."""
        engine = self.engine
        repo = engine.repo
        ticket = repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        ticket.follows.append(Follow(thread=thread, own=False, closed={"at": "context-only"}))
        repo.tickets.save(ticket)

    def mark_unrelated(self, ticket_id: str, thread: Any, reason: Any) -> None:
        """Drop an unrelated context follow and hand back owned follow-ups."""
        engine = self.engine
        repo = engine.repo
        ticket = repo.tickets.get(ticket_id)
        if ticket is None:
            return
        with repo.atomic:
            ticket.follows = [
                item
                for item in ticket.follows
                if not (item.thread.source == thread.source and item.thread.id == thread.id)
            ]
            repo.tickets.save(ticket)

    def handback(self, ticket_id: str, trigger_id: str, reason: Any) -> bool:
        """Release an owned follow-up so it can be evaluated again."""
        engine = self.engine
        repo = engine.repo
        ticket = repo.tickets.get(ticket_id)
        if ticket is None:
            return False
        matching = [item for item in ticket.inputs if item.trigger_id == trigger_id]
        if not matching:
            return False
        with repo.atomic:
            ticket.inputs = [item for item in ticket.inputs if item.trigger_id != trigger_id]
            repo.tickets.save(ticket)
            engine.evals.reevaluate(trigger_id)
        return True

    def policy_changed(self, old: Any, new: Any) -> None:
        """Notify affected live Tickets and requeue running Evals."""
        engine = self.engine
        repo = engine.repo
        old_rules = getattr(old, "rules", {}) or {}
        new_rules = getattr(new, "rules", {}) or {}
        with repo.atomic as tx:
            for ticket in repo.tickets.list_open():
                rules_changed = any(
                    old_rules.get(name) != new_rules.get(name)
                    for name in set(ticket.mandate.matched_rules) | set(old_rules) | set(new_rules)
                )
                affected = (
                    ticket.mandate.policy_version != getattr(new, "version", "") or rules_changed
                )
                if affected:
                    engine.effects.enqueue(
                        "notice",
                        ticket.id,
                        {"text": "Policy changed; review the mandate.", "ticket_id": ticket.id},
                    )
            for thread in repo.threads.list():
                if thread.eval is None:
                    continue
                trigger_ids = list(thread.eval.trigger_ids)
                engine.assessor.release(thread.eval.id)
                thread.eval = None
                thread.queued_trigger_ids.extend(
                    item for item in trigger_ids if item not in thread.queued_trigger_ids
                )
                repo.threads.save(thread)
            tx.after_commit(
                lambda: engine.emit(
                    DomainEvent(DomainEvents.POLICY_CHANGED, {"version": new.version})
                )
            )


__all__ = ["Tickets"]
