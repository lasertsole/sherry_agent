"""Engine ports and domain events (prototype ``src/core/ports.ts``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from superagent.core.model import Receipt, Report, Ticket, Thread, Trigger


@dataclass(frozen=True)
class DomainEvent:
    """An event emitted after a successful transaction."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)


class DomainEvents:
    """Stable domain event names."""

    TRIGGER_RECEIVED = "trigger.received"
    EVAL_STARTED = "eval.started"
    DECISION_RECORDED = "decision.recorded"
    TICKET_CREATED = "ticket.created"
    TICKET_RESUMED = "ticket.resumed"
    TICKET_ENDED = "ticket.ended"
    ASSIGNMENT_STARTED = "assignment.started"
    ASSIGNMENT_ENDED = "assignment.ended"
    THREAD_SUBSCRIBED = "thread.subscribed"
    THREAD_UNSUBSCRIBED = "thread.unsubscribed"
    TRIGGER_REQUEUED = "trigger.requeued"
    POLICY_CHANGED = "policy.changed"


class Atomic(Protocol):
    """Transaction handle with commit-only callbacks."""

    def __enter__(self) -> Atomic: ...

    def __exit__(self, *exc: object) -> None: ...

    def after_commit(self, callback: Callable[[], None]) -> None: ...


class Repositories(Protocol):
    """Pick of repositories used by the Engine."""

    @property
    def atomic(self) -> Atomic: ...

    @property
    def triggers(self) -> Any: ...

    @property
    def threads(self) -> Any: ...

    @property
    def tickets(self) -> Any: ...

    @property
    def decisions(self) -> Any: ...

    @property
    def effects(self) -> Any: ...

    @property
    def outbox(self) -> Any: ...


class PolicyPort(Protocol):
    """Policy surface used by the Engine."""

    version: str
    paused: bool
    eval_timeout_ms: int

    def gate(self, trigger: Trigger, thread: Thread | None, tickets: list[Ticket]) -> Any: ...

    def revalidate(self, mandate: Any) -> Any: ...

    def admits_inferred(self, rules: list[str] | None = None) -> bool: ...


class SourceScope(Protocol):
    """Source-side operations the Engine may call."""

    def receive(self, trigger: Trigger) -> str: ...

    def bind_thread(self, ticket_id: str, thread: Any, own: bool) -> None: ...

    def thread_closed(self, thread: Any, outcome: str, at: str) -> None: ...


class Assessor(Protocol):
    """Candidate assessment port."""

    def request(self, eval_id: str, trigger_ids: list[str]) -> None: ...

    def release(self, eval_id: str) -> None: ...


class Executor(Protocol):
    """Worker execution port."""

    def start(self, ticket_id: str, assignment_id: str) -> None: ...

    def deliver(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None: ...

    def stop(self, ticket_id: str) -> None: ...


class OutboxPort(Protocol):
    """Reply port."""

    def send(self, message: Receipt) -> None: ...

    def status(self, receipt_id: str) -> str | None: ...

    def withdraw(self, receipt_id: str) -> None: ...


class EffectPort(Protocol):
    """Effect enqueue port."""

    def enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> None: ...


class EngineForSources(Protocol):
    """Methods Sources may call."""

    def receive(self, trigger: Trigger) -> str: ...

    def bind_thread(self, ticket_id: str, thread: Any, own: bool) -> None: ...

    def thread_closed(self, thread: Any, outcome: str, at: str) -> None: ...


class EngineForMainSession(Protocol):
    """Methods the main Session may call."""

    def candidate(self, trigger_id: str) -> dict[str, Any] | None: ...

    def candidate_delivered(self, eval_id: str) -> None: ...

    def decide(self, trigger_id: str, outcome: Any, reason: Any = None) -> None: ...

    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> None: ...

    def reply(self, ticket_id: str, purpose: Any, text: str, channel: Any | None = None) -> Any: ...


class EngineForPeople(Protocol):
    """Methods panel/people commands may call."""

    def stop_ticket(self, ticket_id: str, reason: Any = None) -> None: ...

    def retry_ticket(self, ticket_id: str) -> None: ...

    def reevaluate(self, trigger_id: str) -> None: ...

    def dismiss(self, decision_id: str) -> None: ...


class EngineForSettings(Protocol):
    """Methods the Settings writer may call."""

    def policy_changed(self, old: Any, new: Any) -> None: ...


class EngineForExecutor(Protocol):
    """Methods worker adapters may call."""

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None: ...

    def inputs_delivered(
        self, ticket_id: str, assignment_id: str, input_ids: list[str]
    ) -> None: ...

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None: ...

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None: ...

    def delivery_failed(
        self, ticket_id: str, assignment_id: str, error: str, input_id: str | None = None
    ) -> None: ...

    def report(self, ticket_id: str, report: Report) -> None: ...

    def handback(self, ticket_id: str, trigger_id: str, reason: Any) -> bool: ...


__all__ = [
    "Atomic",
    "EngineForExecutor",
    "EngineForMainSession",
    "EngineForPeople",
    "EngineForSettings",
    "EngineForSources",
    "Assessor",
    "DomainEvent",
    "DomainEvents",
    "EffectPort",
    "Executor",
    "OutboxPort",
    "PolicyPort",
    "Repositories",
    "SourceScope",
]
