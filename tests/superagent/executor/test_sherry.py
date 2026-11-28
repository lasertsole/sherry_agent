"""SherryExecutor: worker slots, launch/deliver/stop, retry and resume."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from superagent.app.effects import Later
from superagent.core.memory import MemoryRepositories
from superagent.executor.sherry import (
    SherryExecutor,
    SherryHTTPError,
    SpawnRequest,
    WorkerSlots,
)
from tests.superagent.conftest import make_ticket


@dataclass
class Settings:
    max_workers: int = 1
    executor_model: str = "model-x"
    prepare: str = ""


class Client:
    def __init__(self, *, fail: Exception | None = None, run_id: str = "run-1") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.run_id = run_id

    async def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if self.fail is not None:
            raise self.fail
        return {"run_id": self.run_id}


class Engine:
    def __init__(self, repo: MemoryRepositories) -> None:
        self.repo = repo
        self.started: list[tuple[str, str]] = []
        self.delivered: list[tuple[str, str, list[str]]] = []
        self.failed: list[tuple] = []
        self.reports: list[tuple] = []
        self.interrupted: list[tuple[str, str]] = []

    def get_ticket(self, ticket_id: str):
        return self.repo.tickets.get(ticket_id)

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        self.started.append((ticket_id, assignment_id))

    def inputs_delivered(self, ticket_id: str, assignment_id: str, ids: list[str]) -> None:
        self.delivered.append((ticket_id, assignment_id, ids))

    def delivery_failed(
        self, ticket_id: str, assignment_id: str, error: str, input_id=None
    ) -> None:
        self.failed.append((ticket_id, assignment_id, error))

    def report(self, ticket_id: str, report) -> None:
        self.reports.append((ticket_id, report))

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        self.interrupted.append((ticket_id, assignment_id))


class Effects:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict]] = []
        self.handlers: dict[str, object] = {}

    def enqueue(self, kind: str, key: str, payload: dict) -> None:
        self.items.append((kind, key, payload))

    def handle(self, kind: str, run, *, concurrency: int = 1, retry=None) -> None:
        self.handlers[kind] = run


def build(*, fail: Exception | None = None, max_workers: int = 1):
    repo = MemoryRepositories()
    ticket = make_ticket()
    repo.tickets.save(ticket)
    engine = Engine(repo)
    effects = Effects()
    client = Client(fail=fail)
    host = type("Host", (), {"engine": engine, "effects": effects})()
    executor = SherryExecutor(
        host, client, settings_getter=lambda: Settings(max_workers=max_workers)
    )
    executor.install()
    return executor, engine, effects, client, ticket


@pytest.mark.asyncio
async def test_worker_slots_launch_wake_and_release() -> None:
    slots = WorkerSlots(1)
    await slots.acquire_launch()
    with pytest.raises(Later):
        await slots.acquire_launch()
    assert slots.can_wake_extra is True
    await slots.acquire_wake()
    assert slots.active_count == 2
    with pytest.raises(Later):
        await slots.acquire_wake()
    slots.release()
    slots.release()
    assert slots.active_count == 0


@pytest.mark.asyncio
async def test_launch_spawns_worker_of_main_session() -> None:
    executor, engine, effects, client, ticket = build()
    executor.start(ticket.id, "assign-1")
    assert effects.items[0][0] == "launch"
    await executor._handle_launch(effects.items[0][2])
    path, payload = client.calls[0]
    assert path == "/internal/superagent/spawn"
    assert payload["requester_session_key"] == "superagent-main"
    assert payload["functional_role"] == "executor"
    assert payload["expects_completion_message"] is True
    assert "DSH_SUPERAGENT_TICKET" in payload["env"]
    assert engine.started == [(ticket.id, "assign-1")]
    assert executor.slots.active_count == 1


@pytest.mark.asyncio
async def test_delivery_and_stop_cancel_worker() -> None:
    executor, engine, effects, client, ticket = build()
    await executor._handle_launch({"ticket_id": ticket.id, "assignment_id": "assign-1"})
    executor.deliver(ticket.id, "assign-1", ["input-1"])
    await executor._handle_delivery(effects.items[-1][2])
    assert engine.delivered == [(ticket.id, "assign-1", ["input-1"])]
    executor.stop(ticket.id)
    cancel = [item for item in effects.items if item[0] == "cancel"]
    assert len(cancel) == 1
    await executor._handle_cancel(cancel[0][2])
    assert client.calls[-1][0] == "/internal/superagent/cancel"


@pytest.mark.asyncio
async def test_transient_http_retry_then_hard_failure() -> None:
    executor, _engine, effects, _client, ticket = build(fail=SherryHTTPError(503, "busy"))
    payload = {"ticket_id": ticket.id, "assignment_id": "assign-1"}
    for _ in range(2):
        with pytest.raises(Later):
            await executor._handle_launch(payload)
    with pytest.raises(SherryHTTPError):
        await executor._handle_launch(payload)
    assert executor.slots.active_count == 0


@pytest.mark.asyncio
async def test_non_transient_failure_fails_immediately() -> None:
    executor, _engine, _effects, _client, ticket = build(fail=SherryHTTPError(400, "bad"))
    with pytest.raises(SherryHTTPError):
        await executor._handle_launch({"ticket_id": ticket.id, "assignment_id": "assign-1"})


def test_resume_reports_running_and_requeues_pending_oldest_first() -> None:
    executor, engine, effects, _client, _ticket = build()
    repo = engine.repo
    running = make_ticket("ticket-running")
    running.assignments[0].status = "running"
    running.origin["at"] = "2026-11-28T00:00:00+00:00"
    pending = make_ticket("ticket-pending")
    pending.origin["at"] = "2026-11-28T00:01:00+00:00"
    repo.tickets.save(running)
    repo.tickets.save(pending)
    requeued = executor.resume()
    assert requeued == ["ticket-1", "ticket-running", "ticket-pending"]
    assert engine.interrupted == [("ticket-running", "assign-1")]
    assert any(item[0] == "launch" and item[1] == "ticket-pending" for item in effects.items)


def test_spawn_request_defaults() -> None:
    request = SpawnRequest()
    assert request.requester_session_key == "superagent-main"
    assert request.expects_completion_message is True
    assert request.as_dict()["functional_role"] == "executor"
