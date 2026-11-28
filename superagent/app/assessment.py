"""MasterAssessor (prototype ``src/app/assessment.ts``).

The assessor never calls a model provider: it enqueues an assessment Effect
that steers a candidate card into the main Session turn.  Tool backends read a
bounded context and convert a model verdict into an Engine Decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from superagent.core.model import DecisionOutcome, Reason, Trigger
from superagent.sources.admission_context import ContextMessage, ThreadWindow
from superagent.core.ports import EffectPort, PolicyPort


@dataclass
class EvalContext:
    """Bounded card returned by ``orchestrator_eval``."""

    trigger_text: str
    policy_version: str
    image_ids: list[str] = field(default_factory=list)
    other_triggers_in_batch: list[dict[str, Any]] = field(default_factory=list)
    related_tickets: list[dict[str, Any]] = field(default_factory=list)
    tickets_total: int = 0
    thread_window: ThreadWindow = field(default_factory=ThreadWindow)
    other_messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe card for a tool result."""
        return {
            "trigger_text": self.trigger_text,
            "policy_version": self.policy_version,
            "image_ids": self.image_ids,
            "other_triggers_in_batch": self.other_triggers_in_batch,
            "related_tickets": self.related_tickets,
            "tickets_total": self.tickets_total,
            "thread_window": {
                "first_five": [message.stub() for message in self.thread_window.first_five],
                "around": [message.text for message in self.thread_window.around],
            },
            "other_messages": self.other_messages,
        }


class AssessorHost(Protocol):
    """Surface the assessor needs from the Runtime."""

    engine: Any

    def policy(self) -> PolicyPort: ...


class MasterAssessor:
    """Per-Eval context cache and the assessor port used by the Engine."""

    def __init__(self, host: AssessorHost, effects: EffectPort) -> None:
        self.host = host
        self.effects = effects
        self._cache: dict[str, EvalContext] = {}
        self._messages: dict[str, ContextMessage] = {}

    def request(self, eval_id: str, trigger_ids: list[str]) -> None:
        """Enqueue an assessment card for the main Session."""
        self.effects.enqueue("assessment", eval_id, {"trigger_ids": list(trigger_ids)})

    def release(self, eval_id: str) -> None:
        """Drop the cached card when the Eval ends."""
        self._cache.pop(eval_id, None)

    def context(self, eval_id: str) -> EvalContext:
        """Return (and cache) the bounded card for an Eval."""
        if eval_id not in self._cache:
            self._cache[eval_id] = self._build_context(eval_id)
        return self._cache[eval_id]

    def read(self, eval_id: str, message_id: str) -> ContextMessage:
        """Expand one stub message by id."""
        self.context(eval_id)
        message = self._messages.get(message_id)
        if message is None:
            raise KeyError(message_id)
        return message

    def decide(self, eval_id: str, outcome: DecisionOutcome, reason: Reason) -> None:
        """Forward a model verdict to the Engine and release the card."""
        context = self.context(eval_id)
        trigger_id = (
            context.other_triggers_in_batch[0].get("id", "")
            if context.other_triggers_in_batch
            else ""
        )
        self.host.engine.decide(trigger_id or eval_id, outcome, reason)
        self.release(eval_id)

    def _build_context(self, eval_id: str) -> EvalContext:
        engine = self.host.engine
        record = next(
            (
                thread.eval
                for thread in engine.repo.threads.list()
                if thread.eval is not None and thread.eval.id == eval_id
            ),
            None,
        )
        trigger_ids: list[str] = list(getattr(record, "trigger_ids", [eval_id]))
        triggers: list[Trigger] = [
            item
            for trigger_id in trigger_ids
            if (item := engine.repo.triggers.get(trigger_id)) is not None
        ]
        primary = triggers[0] if triggers else None
        text = primary.text[:4000] if primary is not None else ""
        others = [{"id": item.id, "text": item.text[:200], "at": item.at} for item in triggers[1:]]
        tickets = [ticket for ticket in engine.repo.tickets.list() if ticket.is_open]
        related = [
            {
                "id": ticket.id,
                "stage": str(ticket.latest_report.kind if ticket.latest_report else "queued"),
            }
            for ticket in tickets[:8]
        ]
        window = ThreadWindow()
        stubs: list[str] = []
        for message in window.all_messages():
            self._messages[message.id] = message
            stubs.append(message.stub())
        return EvalContext(
            trigger_text=text,
            policy_version=str(self.host.policy().version),
            other_triggers_in_batch=others,
            related_tickets=related,
            tickets_total=len(tickets),
            thread_window=window,
            other_messages=stubs,
        )


def assessment_outcome(
    verdict: dict[str, Any],
    *,
    accept_mode: str,
    strength: str,
) -> tuple[DecisionOutcome, Reason]:
    """Convert a model verdict into an Engine outcome, fail-closed on error."""
    if verdict.get("error"):
        return DecisionOutcome.UNAVAILABLE, Reason(
            "assessmentFailed", {"detail": str(verdict["error"])[:200]}
        )
    matched = [str(item) for item in verdict.get("matched_rules", []) if item]
    if not matched:
        return DecisionOutcome.IGNORE, Reason("ignoredFromCard", {})
    if strength == "explicit":
        return DecisionOutcome.ACCEPT, Reason("accepted", {"rules": matched})
    if accept_mode == "auto":
        return DecisionOutcome.ACCEPT, Reason("assessmentAccept", {"rules": matched})
    if accept_mode == "observe":
        return DecisionOutcome.IGNORE, Reason("wouldAccept", {"rules": matched})
    return DecisionOutcome.IGNORE, Reason("acceptOff", {})


__all__ = ["AssessorHost", "EvalContext", "MasterAssessor", "assessment_outcome"]
