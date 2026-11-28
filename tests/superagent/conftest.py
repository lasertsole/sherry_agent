"""Shared factories for superagent P0 tests."""

from __future__ import annotations

import pytest

from superagent.core.model import (
    Actor,
    Assignment,
    Channel,
    ChannelAddress,
    Mandate,
    MandateBasis,
    Receipt,
    Report,
    ReportKind,
    SourceRef,
    Strength,
    Thread,
    Ticket,
    Trigger,
)


def ref(source: str = "github", id_: str = "thread-1") -> SourceRef:
    """Build a SourceRef."""
    return SourceRef(source=source, id=id_)


def make_trigger(
    id_: str = "trig-1",
    at: str = "2026-11-28T00:00:00+00:00",
    text: str = "please handle this",
    source: str = "github",
) -> Trigger:
    """Build a Trigger with one primary Thread."""
    thread = ref(source, "thread-1")
    return Trigger(
        id=id_,
        source=source,
        threads=[thread],
        actor=Actor(source=source, id="alice"),
        human=True,
        addressed=None,
        at=at,
        text=text,
        channel=ChannelAddress(source=source, thread=thread.id, address={"issue": 1}),
        url=f"https://example.test/{id_}",
        meta={"delivery": "webhook"},
    )


def make_thread(key: str = "thread-key") -> Thread:
    """Build a Thread with no queue and no Eval."""
    return Thread(key=key, source="github", id="thread-1")


def make_ticket(id_: str = "ticket-1") -> Ticket:
    """Build an open Ticket with one pending Assignment."""
    return Ticket(
        id=id_,
        origin={"trigger_id": "trig-1", "trigger_ids": ["trig-1"]},
        mandate=Mandate(
            policy_version="v1",
            scope="github",
            strength=Strength.EXPLICIT,
            matched_rules=["rule-1"],
            sender=Actor(source="github", id="alice"),
            via=MandateBasis.ADDRESSED,
            basis_detail={"sender": "alice"},
        ),
        execution={"name": "default", "directory": "/tmp/work"},
        channels=[ChannelAddress(source="github", thread="thread-1", address={"issue": 1})],
        inputs=[],
        follows=[],
        assignments=[Assignment(id="assign-1", ticket_id=id_)],
        latest_report=None,
    )


def make_receipt(id_: str = "msg-1", ticket_id: str = "ticket-1") -> Receipt:
    """Build an outbox Receipt."""
    return Receipt(
        id=id_,
        ticket_id=ticket_id,
        channel=ChannelAddress(source="github", thread="thread-1", address={"issue": 1}),
        purpose=Channel.RESULT,
        text="done",
    )


def make_report(
    kind: ReportKind = ReportKind.PROGRESS, at: str = "2026-11-28T01:00:00+00:00"
) -> Report:
    """Build a worker Report."""
    return Report(assignment_id="assign-1", kind=kind, summary="working", at=at)


@pytest.fixture
def sample_trigger() -> Trigger:
    """Return a fresh Trigger."""
    return make_trigger()


@pytest.fixture
def sample_thread() -> Thread:
    """Return a fresh Thread."""
    return make_thread()


@pytest.fixture
def sample_ticket() -> Ticket:
    """Return a fresh Ticket."""
    return make_ticket()


class FakeEffects:
    """Record Effect enqueues without running handlers."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict]] = []

    def enqueue(self, kind: str, key: str, payload: dict) -> None:
        self.items.append((kind, key, payload))

    def count(self, kind: str) -> int:
        return sum(1 for item in self.items if item[0] == kind)

    def handle(self, kind: str, run, *, concurrency: int = 1, retry=None) -> None:
        """Record an effect handler (used by the P2 Executor seam)."""
        if not hasattr(self, "handlers"):
            self.handlers = {}
        self.handlers[kind] = run


class FakeOutbox:
    """Persist Receipts through the repository."""

    def __init__(self, repo) -> None:
        self.repo = repo

    def send(self, message) -> None:
        self.repo.outbox.save(message)

    def status(self, receipt_id: str):
        receipt = self.repo.outbox.get(receipt_id)
        return receipt.status if receipt else None

    def withdraw(self, receipt_id: str) -> None:
        self.repo.outbox.set_dropped(receipt_id, "channelIgnored")


class FakeScope:
    """Record SourceScope calls."""

    def __init__(self) -> None:
        self.bound: list[tuple] = []
        self.closed: list[tuple] = []

    def receive(self, trigger) -> str:
        return "stored"

    def bind_thread(self, ticket_id, thread, own) -> None:
        self.bound.append((ticket_id, thread, own))

    def thread_closed(self, thread, outcome, at) -> None:
        self.closed.append((thread, outcome, at))


class FakeAssessor:
    """Record assessment requests."""

    def __init__(self) -> None:
        self.requested: list[tuple] = []
        self.released: list[str] = []

    def request(self, eval_id: str, trigger_ids: list[str]) -> None:
        self.requested.append((eval_id, trigger_ids))

    def release(self, eval_id: str) -> None:
        self.released.append(eval_id)


class FakeExecutor:
    """Record worker commands."""

    def __init__(self) -> None:
        self.started: list[tuple] = []
        self.delivered: list[tuple] = []
        self.stopped: list[str] = []

    def start(self, ticket_id: str, assignment_id: str) -> None:
        self.started.append((ticket_id, assignment_id))

    def deliver(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        self.delivered.append((ticket_id, assignment_id, input_ids))

    def stop(self, ticket_id: str) -> None:
        self.stopped.append(ticket_id)


def policy_settings(accept_mode: str = "observe", receive_all: bool = True) -> dict:
    """Return deterministic Policy settings for tests."""
    return {
        "accept_mode": accept_mode,
        "paused": False,
        "eval_timeout_ms": 600_000,
        "listening": ["github"],
        "rules": {"rule-1": {"enabled": True, "keywords": ["please"]}},
        "sources": {
            "github": {
                "allowed": [{"id": "alice"}],
                "bots": [{"id": "dependabot[bot]"}],
                "receive_all": receive_all,
                "admission": {
                    "github/issue": {
                        "listed_unaddressed": "inferred",
                        "unlisted_addressed": "ignore",
                        "unlisted_unaddressed": "ignore",
                    }
                },
            }
        },
    }


def make_engine(settings: dict | None = None, repo=None, now=None):
    """Build an Engine with recorded ports and a MemoryRepositories backend."""
    from superagent.core.engine import Engine
    from superagent.core.memory import MemoryRepositories
    from superagent.core.part import EngineOptions
    from superagent.policy import settings_policy

    settings = settings or policy_settings()
    repo = repo or MemoryRepositories()
    effects = FakeEffects()
    scope = FakeScope()
    assessor = FakeAssessor()
    executor = FakeExecutor()
    outbox = FakeOutbox(repo)
    events: list = []
    opts = EngineOptions(
        repo=repo,
        policy_get=lambda: settings_policy(settings, "github"),
        scope=scope,
        assessor=assessor,
        executor=executor,
        outbox=outbox,
        effects=effects,
        prepare_ticket=lambda trigger, gate: {"name": "default", "directory": "/tmp/work"},
        now=now,
        emit=events.append,
    )
    engine = Engine(opts)
    return engine, {
        "repo": repo,
        "effects": effects,
        "scope": scope,
        "assessor": assessor,
        "executor": executor,
        "outbox": outbox,
        "events": events,
        "settings": settings,
    }
