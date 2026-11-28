"""Engine façade and role methods (prototype ``src/core/engine.py``)."""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from superagent.core.assignments import Assignments
from superagent.core.errors import UserError
from superagent.core.evals import Evals
from superagent.core.ids import thread_key as canonical_thread_key
from superagent.core.intake import Intake
from superagent.core.model import Report, SourceRef, Ticket, Trigger
from superagent.core.part import EngineOptions
from superagent.core.ports import DomainEvent
from superagent.core.replies import Replies
from superagent.core.tickets import Tickets


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Engine:
    """Synchronous single writer of Triggers, Threads, Decisions and Tickets."""

    def __init__(self, opts: EngineOptions) -> None:
        self.repo = opts.repo
        self.policy_get = opts.policy_get
        self.scope = opts.scope
        self.assessor = opts.assessor
        self.executor = opts.executor
        self.outbox = opts.outbox
        self.effects = opts.effects
        self.prepare_ticket = opts.prepare_ticket
        self.now = opts.now or _utc_now
        self.emit = opts.emit or (lambda event: None)
        self.replies = Replies(self)
        self.tickets = Tickets(self)
        self.assignments = Assignments(self)
        self.evals = Evals(self)
        self.intake = Intake(self)

    @staticmethod
    def thread_key(ref: SourceRef) -> str:
        """Return the canonical key for a Thread reference."""
        return canonical_thread_key(ref)

    # Sources role
    def receive(self, trigger: Trigger) -> str:
        """Receive a Trigger."""
        return self.intake.receive(trigger)

    def bind_thread(self, ticket_id: str, thread: SourceRef, own: bool) -> None:
        """Bind a Ticket to a Thread."""
        self.intake.bind_thread(ticket_id, thread, own)

    def thread_closed(self, thread: SourceRef, outcome: str, at: str) -> None:
        """Record that a followed Thread closed."""
        self.intake.thread_closed(thread, outcome, at)

    # Main Session role
    def candidate(self, trigger_id: str) -> dict[str, Any] | None:
        """Return an assessment card."""
        return self.evals.candidate(trigger_id)

    def candidate_delivered(self, eval_id: str) -> None:
        """Start an Eval deadline after delivery."""
        self.evals.candidate_delivered(eval_id)

    def decide(self, trigger_id: str, outcome: Any, reason: Any = None) -> None:
        """Record an assessment Decision."""
        self.evals.decide(trigger_id, outcome, reason)

    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> None:
        """Attach an explicitly addressed Trigger to a Ticket."""
        self.tickets.attach(ticket_id, trigger_id, note)

    def request_status(self, ticket_id: str) -> bool:
        """Reply with a Ticket's current stage."""
        return self.replies.request_status(ticket_id)

    def note(self, ticket_id: str, text: str) -> None:
        """Add a context-only note."""
        self.replies.note(ticket_id, text)

    def reply(self, ticket_id: str, purpose: Any, text: str, channel: Any | None = None) -> Any:
        """Send a Channel reply."""
        return self.replies.reply(ticket_id, purpose, text, channel)

    # People role
    def stop_ticket(self, ticket_id: str, reason: Any = None) -> None:
        """Permanently stop a Ticket."""
        ticket = self.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        self.tickets.stop(ticket, reason)

    def retry_ticket(self, ticket_id: str) -> None:
        """Retry a blocked/failed or delivery-failed Ticket."""
        ticket = self.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("not_found", {"ticket_id": ticket_id})
        self.tickets.retry(ticket)

    def reevaluate(self, trigger_id: str) -> None:
        """Requeue a Trigger."""
        self.evals.reevaluate(trigger_id)

    def unsubscribe(self, thread: SourceRef) -> None:
        """Remove all Ticket subscriptions for a Thread."""
        self.tickets.unsubscribe(thread)

    def dismiss(self, decision_id: str) -> None:
        """Dismiss an unavailable Decision."""
        self.evals.dismiss(decision_id)

    # Executor role
    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        """Record a started Assignment."""
        self.assignments.assignment_started(ticket_id, assignment_id)

    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        """Record delivered Inputs."""
        self.assignments.inputs_delivered(ticket_id, assignment_id, input_ids)

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        """Record read Inputs."""
        self.assignments.inputs_read(ticket_id, input_ids)

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        """Record a finished turn."""
        self.assignments.turn_ended(ticket_id, assignment_id)

    def delivery_failed(
        self,
        ticket_id: str,
        assignment_id: str,
        error: str,
        input_id: str | None = None,
    ) -> None:
        """Record a failed Input delivery."""
        self.assignments.delivery_failed(ticket_id, assignment_id, error, input_id)

    def report(self, ticket_id: str, report: Report) -> None:
        """Record a worker Report."""
        self.assignments.report(ticket_id, report)

    def mark_unrelated(self, ticket_id: str, thread: SourceRef, reason: Any) -> None:
        """Release an unrelated context follow."""
        self.tickets.mark_unrelated(ticket_id, thread, reason)

    def follow_for_context(self, ticket_id: str, thread: SourceRef) -> None:
        """Follow a Thread for context only."""
        self.tickets.follow_for_context(ticket_id, thread)

    def handback(self, ticket_id: str, trigger_id: str, reason: Any) -> bool:
        """Hand a follow-up back for a fresh Eval."""
        return self.tickets.handback(ticket_id, trigger_id, reason)

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        """Restart an interrupted Assignment."""
        self.assignments.assignment_interrupted(ticket_id, assignment_id)

    # Passes role
    def evaluate_due(self) -> None:
        """Run the Eval pass."""
        self.evals.evaluate_due()

    # Settings role
    def policy_changed(self, old: Any, new: Any) -> None:
        """Apply a Policy version change."""
        self.tickets.policy_changed(old, new)

    def emit_event(self, event: DomainEvent) -> None:
        """Broadcast a domain event."""
        self.emit(event)

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        """Return a Ticket for adapter use."""
        return self.repo.tickets.get(ticket_id)


__all__ = ["Engine"]
