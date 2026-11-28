"""Domain types and shared predicates (prototype ``src/core/model.ts``).

Dataclasses are plain data; validation lives in pydantic schemas at the edge.
Only the Python standard library is imported here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any


class Strength(StrEnum):
    """How strongly a sender is authorized."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class DecisionOutcome(StrEnum):
    """Outcome of an Eval."""

    ACCEPT = "accept"
    STOP = "stop"
    IGNORE = "ignore"
    UNAVAILABLE = "unavailable"


class AssignmentStatus(StrEnum):
    """Lifecycle of one worker assignment."""

    PENDING = "pending"
    RUNNING = "running"
    ENDED = "ended"


class ReportKind(StrEnum):
    """Kind of report a worker sends back."""

    PROGRESS = "progress"
    WAITING = "waiting"
    READY = "ready"
    ATTENTION = "attention"
    BLOCKED = "blocked"
    FAILED = "failed"
    DROPPED = "dropped"


class AddressKind(StrEnum):
    """How a person addressed the agent."""

    MENTION = "mention"
    DIRECT = "direct"


class MandateBasis(StrEnum):
    """Why a Mandate was granted."""

    ADDRESSED = "addressed"
    FOLLOWUP = "followup"
    ADMISSION = "admission"
    BOT = "bot"


class AcceptMode(StrEnum):
    """Policy accept mode."""

    OFF = "off"
    OBSERVE = "observe"
    AUTO = "auto"


class Channel(StrEnum):
    """Purpose of an outbox message."""

    ACKNOWLEDGEMENT = "acknowledgement"
    ANSWER = "answer"
    RESULT = "result"
    ATTENTION = "attention"


class TicketOutcome(StrEnum):
    """How a Ticket ended."""

    MERGED = "merged"
    CLOSED = "closed"
    COMPLETED = "completed"
    DROPPED = "dropped"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SourceRef:
    """A Source name plus the Source's own id."""

    source: str
    id: str


@dataclass(frozen=True)
class Actor:
    """A sender; an empty id means CI or another sender-less actor."""

    source: str
    id: str


@dataclass(frozen=True)
class ChannelAddress:
    """A reply destination with Source-specific address fields."""

    source: str
    thread: str
    address: dict[str, Any]


@dataclass(frozen=True)
class Trigger:
    """One immutable inbound event."""

    id: str
    source: str
    threads: list[SourceRef]
    actor: Actor
    human: bool
    addressed: AddressKind | None
    at: str
    text: str
    channel: ChannelAddress | None
    url: str | None
    meta: dict[str, Any]


@dataclass(frozen=True)
class Correlation:
    """Additional Thread membership for a Trigger."""

    trigger_id: str
    threads: list[SourceRef]


@dataclass
class Subscription:
    """A Thread subscribed by a Ticket."""

    ticket_id: str
    thread: SourceRef
    own: bool = False
    context_only: bool = False


@dataclass
class Eval:
    """At most one Eval per Thread; released after a Decision."""

    id: str
    thread: SourceRef
    primary: str
    trigger_ids: list[str]
    delivered: bool = False
    deadline: datetime | None = None


@dataclass(frozen=True)
class Reason:
    """A stable reason code with template values."""

    code: str
    values: dict[str, Any]
    trigger_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """A recorded Eval outcome."""

    id: str
    thread: SourceRef
    outcome: DecisionOutcome
    reason: Reason
    at: str
    trigger_ids: list[str]


@dataclass(frozen=True)
class Mandate:
    """Immutable authorization granted by an accept Decision."""

    policy_version: str
    scope: str
    strength: Strength
    matched_rules: list[str]
    sender: Actor
    via: MandateBasis
    basis_detail: dict[str, Any]


@dataclass
class Follow:
    """A Thread followed by a Ticket."""

    thread: SourceRef
    own: bool = False
    closed: dict[str, Any] | None = None


@dataclass
class Input:
    """Context delivered to an Executor session."""

    id: str
    ticket_id: str
    kind: str
    trigger_id: str | None
    text: str | None
    human: bool = False
    delivered: bool = False
    read: bool = False
    at: str = ""
    attempts: int = 0
    last_error: str | None = None


@dataclass
class Report:
    """A worker report attached to an Assignment."""

    assignment_id: str
    kind: ReportKind
    summary: str
    at: str
    question: str | None = None
    pull_request: dict[str, Any] | None = None


@dataclass
class Assignment:
    """One goal-bounded worker run."""

    id: str
    ticket_id: str
    status: AssignmentStatus = AssignmentStatus.PENDING
    started_at: str | None = None
    ended_at: str | None = None
    latest_report: Report | None = None


@dataclass
class Ticket:
    """The unit of delegated work."""

    id: str
    origin: dict[str, Any]
    mandate: Mandate
    execution: dict[str, Any]
    channels: list[ChannelAddress]
    inputs: list[Input]
    follows: list[Follow]
    assignments: list[Assignment]
    latest_report: Report | None
    ended: dict[str, Any] | None = None
    last_said: str | None = None

    @property
    def ended_at(self) -> str | None:
        """Explicit column value derived from ``ended``."""
        if self.ended is None:
            return None
        value = self.ended.get("at")
        return str(value) if value is not None else None

    @property
    def latest_assignment_status(self) -> str | None:
        """Explicit column value derived from the last Assignment."""
        if not self.assignments:
            return None
        value = self.assignments[-1].status
        return value.value if isinstance(value, Enum) else str(value)

    @property
    def has_undelivered_input(self) -> bool:
        """Explicit column value derived from Inputs."""
        return any(not item.delivered for item in self.inputs)

    @property
    def is_open(self) -> bool:
        """Whether the Ticket is still open."""
        return self.ended is None

    @property
    def is_active(self) -> bool:
        """Whether the Ticket has pending work."""
        return self.is_open and (
            self.has_undelivered_input
            or self.latest_assignment_status
            in (AssignmentStatus.PENDING.value, AssignmentStatus.RUNNING.value)
        )


@dataclass
class Receipt:
    """A durable outbox message."""

    id: str
    ticket_id: str | None
    channel: ChannelAddress
    purpose: Channel
    text: str
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    dropped: dict[str, Any] | None = None


@dataclass
class Effect:
    """A durable queued side effect (prototype ``src/app/effects.ts``)."""

    kind: str
    key: str
    payload: dict[str, Any]
    seq: int | None = None
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    due_at: str | None = None


@dataclass
class Thread:
    """A source thread with subscriptions, queue and optional Eval."""

    key: str
    source: str
    id: str
    subscriptions: list[Subscription] = field(default_factory=list)
    context: dict[str, Any] | None = None
    queued_trigger_ids: list[str] = field(default_factory=list)
    eval: Eval | None = None
    expired_count: int = 0
    meta: dict[str, Any] | None = None

    @property
    def has_queued(self) -> bool:
        """Explicit column value derived from the queue."""
        return bool(self.queued_trigger_ids)

    @property
    def has_eval(self) -> bool:
        """Explicit column value derived from the Eval."""
        return self.eval is not None

    @property
    def is_subscribed(self) -> bool:
        """Explicit column value derived from subscriptions."""
        return bool(self.subscriptions)

    @property
    def is_active(self) -> bool:
        """Whether the Thread has queued work or a live Eval."""
        return self.has_queued or self.has_eval


def _plain(value: Any) -> Any:
    """Convert dataclasses, enums and datetimes into JSON-safe values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def to_dict(value: Any) -> Any:
    """Return the JSON-safe form of a domain object."""
    return _plain(value)


def _enum[E: Enum](enum_type: type[E], value: Any, default: E) -> E:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _opt_enum[E: Enum](enum_type: type[E], value: Any) -> E | None:
    if value is None or isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return None


def _obj(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def source_ref_from_dict(data: dict[str, Any]) -> SourceRef:
    """Rebuild a SourceRef from stored JSON."""
    return SourceRef(source=str(data.get("source", "")), id=str(data.get("id", "")))


def actor_from_dict(data: dict[str, Any]) -> Actor:
    """Rebuild an Actor from stored JSON."""
    return Actor(source=str(data.get("source", "")), id=str(data.get("id", "")))


def channel_address_from_dict(data: dict[str, Any] | None) -> ChannelAddress | None:
    """Rebuild a ChannelAddress from stored JSON."""
    if data is None:
        return None
    return ChannelAddress(
        source=str(data.get("source", "")),
        thread=str(data.get("thread", "")),
        address=_obj(data.get("address")),
    )


def trigger_from_dict(data: dict[str, Any]) -> Trigger:
    """Rebuild a Trigger from stored JSON."""
    return Trigger(
        id=str(data.get("id", "")),
        source=str(data.get("source", "")),
        threads=[source_ref_from_dict(item) for item in _items(data.get("threads"))],
        actor=actor_from_dict(_obj(data.get("actor"))),
        human=bool(data.get("human", False)),
        addressed=_opt_enum(AddressKind, data.get("addressed")),
        at=str(data.get("at", "")),
        text=str(data.get("text", "")),
        channel=channel_address_from_dict(data.get("channel")),
        url=data.get("url"),
        meta=_obj(data.get("meta")),
    )


def reason_from_dict(data: dict[str, Any]) -> Reason:
    """Rebuild a Reason from stored JSON."""
    return Reason(
        code=str(data.get("code", "")),
        values=_obj(data.get("values")),
        trigger_id=data.get("trigger_id"),
    )


def decision_from_dict(data: dict[str, Any]) -> Decision:
    """Rebuild a Decision from stored JSON."""
    return Decision(
        id=str(data.get("id", "")),
        thread=source_ref_from_dict(_obj(data.get("thread"))),
        outcome=_enum(
            DecisionOutcome,
            data.get("outcome", DecisionOutcome.IGNORE.value),
            DecisionOutcome.IGNORE,
        ),
        reason=reason_from_dict(_obj(data.get("reason"))),
        at=str(data.get("at", "")),
        trigger_ids=[str(item) for item in _items(data.get("trigger_ids"))],
    )


def eval_from_dict(data: dict[str, Any] | None) -> Eval | None:
    """Rebuild an Eval from stored JSON."""
    if data is None:
        return None
    deadline_raw = data.get("deadline")
    deadline = (
        datetime.fromisoformat(deadline_raw)
        if isinstance(deadline_raw, str) and deadline_raw
        else None
    )
    return Eval(
        id=str(data.get("id", "")),
        thread=source_ref_from_dict(_obj(data.get("thread"))),
        primary=str(data.get("primary", "")),
        trigger_ids=[str(item) for item in _items(data.get("trigger_ids"))],
        delivered=bool(data.get("delivered", False)),
        deadline=deadline,
    )


def mandate_from_dict(data: dict[str, Any]) -> Mandate:
    """Rebuild a Mandate from stored JSON."""
    return Mandate(
        policy_version=str(data.get("policy_version", "")),
        scope=str(data.get("scope", "")),
        strength=_enum(Strength, data.get("strength", Strength.INFERRED.value), Strength.INFERRED),
        matched_rules=[str(item) for item in _items(data.get("matched_rules"))],
        sender=actor_from_dict(_obj(data.get("sender"))),
        via=_enum(
            MandateBasis, data.get("via", MandateBasis.ADMISSION.value), MandateBasis.ADMISSION
        ),
        basis_detail=_obj(data.get("basis_detail")),
    )


def subscription_from_dict(data: dict[str, Any]) -> Subscription:
    """Rebuild a Subscription from stored JSON."""
    return Subscription(
        ticket_id=str(data.get("ticket_id", "")),
        thread=source_ref_from_dict(_obj(data.get("thread"))),
        own=bool(data.get("own", False)),
        context_only=bool(data.get("context_only", False)),
    )


def follow_from_dict(data: dict[str, Any]) -> Follow:
    """Rebuild a Follow from stored JSON."""
    return Follow(
        thread=source_ref_from_dict(_obj(data.get("thread"))),
        own=bool(data.get("own", False)),
        closed=data.get("closed"),
    )


def input_from_dict(data: dict[str, Any]) -> Input:
    """Rebuild an Input from stored JSON."""
    return Input(
        id=str(data.get("id", "")),
        ticket_id=str(data.get("ticket_id", "")),
        kind=str(data.get("kind", "text")),
        trigger_id=data.get("trigger_id"),
        text=data.get("text"),
        human=bool(data.get("human", False)),
        delivered=bool(data.get("delivered", False)),
        read=bool(data.get("read", False)),
        at=str(data.get("at", "")),
        attempts=int(data.get("attempts", 0)),
        last_error=data.get("last_error"),
    )


def report_from_dict(data: dict[str, Any] | None) -> Report | None:
    """Rebuild a Report from stored JSON."""
    if data is None:
        return None
    return Report(
        assignment_id=str(data.get("assignment_id", "")),
        kind=_enum(ReportKind, data.get("kind", ReportKind.PROGRESS.value), ReportKind.PROGRESS),
        summary=str(data.get("summary", "")),
        at=str(data.get("at", "")),
        question=data.get("question"),
        pull_request=data.get("pull_request"),
    )


def assignment_from_dict(data: dict[str, Any]) -> Assignment:
    """Rebuild an Assignment from stored JSON."""
    return Assignment(
        id=str(data.get("id", "")),
        ticket_id=str(data.get("ticket_id", "")),
        status=_enum(
            AssignmentStatus,
            data.get("status", AssignmentStatus.PENDING.value),
            AssignmentStatus.PENDING,
        ),
        started_at=data.get("started_at"),
        ended_at=data.get("ended_at"),
        latest_report=report_from_dict(data.get("latest_report")),
    )


def ticket_from_dict(data: dict[str, Any]) -> Ticket:
    """Rebuild a Ticket from stored JSON."""
    return Ticket(
        id=str(data.get("id", "")),
        origin=_obj(data.get("origin")),
        mandate=mandate_from_dict(_obj(data.get("mandate"))),
        execution=_obj(data.get("execution")),
        channels=[
            channel
            for item in _items(data.get("channels"))
            if (channel := channel_address_from_dict(_obj(item))) is not None
        ],
        inputs=[input_from_dict(_obj(item)) for item in _items(data.get("inputs"))],
        follows=[follow_from_dict(_obj(item)) for item in _items(data.get("follows"))],
        assignments=[assignment_from_dict(_obj(item)) for item in _items(data.get("assignments"))],
        latest_report=report_from_dict(data.get("latest_report")),
        ended=data.get("ended"),
        last_said=data.get("last_said"),
    )


def receipt_from_dict(data: dict[str, Any]) -> Receipt:
    """Rebuild a Receipt from stored JSON."""
    channel = channel_address_from_dict(_obj(data.get("channel")))
    if channel is None:
        channel = ChannelAddress(source="", thread="", address={})
    return Receipt(
        id=str(data.get("id", "")),
        ticket_id=data.get("ticket_id"),
        channel=channel,
        purpose=_enum(Channel, data.get("purpose", Channel.RESULT.value), Channel.RESULT),
        text=str(data.get("text", "")),
        status=str(data.get("status", "pending")),
        attempts=int(data.get("attempts", 0)),
        last_error=data.get("last_error"),
        dropped=data.get("dropped"),
    )


def effect_from_dict(data: dict[str, Any]) -> Effect:
    """Rebuild an Effect from stored JSON."""
    return Effect(
        kind=str(data.get("kind", "")),
        key=str(data.get("key", "")),
        payload=_obj(data.get("payload")),
        seq=data.get("seq"),
        status=str(data.get("status", "pending")),
        attempts=int(data.get("attempts", 0)),
        last_error=data.get("last_error"),
        due_at=data.get("due_at"),
    )


def thread_from_dict(data: dict[str, Any]) -> Thread:
    """Rebuild a Thread from stored JSON."""
    return Thread(
        key=str(data.get("key", "")),
        source=str(data.get("source", "")),
        id=str(data.get("id", "")),
        subscriptions=[
            subscription_from_dict(_obj(item)) for item in _items(data.get("subscriptions"))
        ],
        context=data.get("context"),
        queued_trigger_ids=[str(item) for item in _items(data.get("queued_trigger_ids"))],
        eval=eval_from_dict(data.get("eval")),
        expired_count=int(data.get("expired_count", 0)),
        meta=data.get("meta"),
    )


def waits_for_person(ticket: Ticket) -> bool:
    """Return whether the latest report needs a person."""
    if ticket.latest_report is None:
        return False
    return ticket.latest_report.kind in (ReportKind.BLOCKED, ReportKind.FAILED)


def live_ticket(ticket: Ticket) -> bool:
    """Return whether the Ticket is open."""
    return ticket.ended is None


def running_assignment(ticket: Ticket) -> Assignment | None:
    """Return the newest running Assignment, if any."""
    for assignment in reversed(ticket.assignments):
        if assignment.status == AssignmentStatus.RUNNING:
            return assignment
    return None


def latest_assignment(ticket: Ticket) -> Assignment | None:
    """Return the newest Assignment, if any."""
    return ticket.assignments[-1] if ticket.assignments else None


def settled(decision: Decision | None) -> bool:
    """Return whether a Decision resolved its request."""
    if decision is None:
        return False
    return decision.outcome in (DecisionOutcome.ACCEPT, DecisionOutcome.IGNORE)


def _ended_by_person(ticket: Ticket) -> bool:
    if ticket.ended is None:
        return False
    by = str(ticket.ended.get("by", "")).lower()
    return by in {"human", "person", "people"} or ticket.ended.get("why") == "stop"


def _owns_followup(trigger_id: str, ticket: Ticket) -> bool:
    if ticket.ended is not None and _ended_by_person(ticket):
        return False
    origin_trigger = ticket.origin.get("trigger_id")
    origin_ids = ticket.origin.get("trigger_ids", [])
    if trigger_id == origin_trigger or trigger_id in _items(origin_ids):
        return True
    return any(item.trigger_id == trigger_id for item in ticket.inputs)


def followup_owners(trigger_id: str, tickets: list[Ticket]) -> list[str]:
    """Return Ticket ids that still own a person's follow-up."""
    return [ticket.id for ticket in tickets if _owns_followup(trigger_id, ticket)]


def reached(trigger_id: str, ticket: Ticket) -> bool:
    """Return whether a Trigger reached this Ticket."""
    if trigger_id == ticket.origin.get("trigger_id"):
        return True
    if trigger_id in _items(ticket.origin.get("trigger_ids")):
        return True
    return any(item.trigger_id == trigger_id for item in ticket.inputs)
