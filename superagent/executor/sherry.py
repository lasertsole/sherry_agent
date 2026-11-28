"""Sherry-backed Executor (prototype ``src/executor/dsh.ts``).

Workers are Sherry subagents of a persistent ``superagent-main`` session.
This module never imports ``agent/``: every Sherry operation goes through an
HTTP client port so it can be replaced by a fake in tests.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from superagent.app.effects import Later
from superagent.app.execution_environment import execution_environment
from superagent.core.model import Report, ReportKind, Ticket
from superagent.limits import EFFECT_RETRY_LAUNCH

TRANSIENT_CODES = frozenset({408, 429, 500, 502, 503, 504})
TRANSIENT_RETRIES = 3
PREPARE_TIMEOUT_S = 600
WORK_BUDGET_S = 1800


class SherryHTTPError(RuntimeError):
    """A non-2xx response from the Sherry loopback API."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(f"sherry http {status}: {detail[:200]}")

    @property
    def transient(self) -> bool:
        """Whether this status should be retried."""
        return self.status in TRANSIENT_CODES


class SherryClient(Protocol):
    """HTTP loopback port used by the Executor."""

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, path: str) -> dict[str, Any]: ...


class AiohttpSherryClient:
    """Real loopback client; kept small so no Sherry code is imported."""

    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a Sherry loopback endpoint."""
        import aiohttp

        headers = {"token": self.token} if self.token else {}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(f"{self.base_url}{path}", json=payload) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise SherryHTTPError(response.status, str(body))
                return body if isinstance(body, dict) else {}

    async def get(self, path: str) -> dict[str, Any]:
        """GET JSON from a Sherry loopback endpoint."""
        import aiohttp

        headers = {"token": self.token} if self.token else {}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{self.base_url}{path}") as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise SherryHTTPError(response.status, str(body))
                return body if isinstance(body, dict) else {}


@dataclass(frozen=True)
class SpawnRequest:
    """Arguments for one worker subagent run."""

    requester_session_key: str = "superagent-main"
    functional_role: str = "executor"
    step_model: str = ""
    run_timeout_seconds: int = WORK_BUDGET_S
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    expects_completion_message: bool = True
    announce: bool = True
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe request body."""
        return asdict(self)


class WorkerSlots:
    """Bounded worker concurrency with one extra wake slot."""

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.max = max_workers
        self.running = 0
        self._lock = asyncio.Lock()

    async def acquire_launch(self) -> None:
        """Take a normal slot; raise Later when saturated."""
        async with self._lock:
            if self.running >= self.max:
                raise Later()
            self.running += 1

    async def acquire_wake(self) -> None:
        """Take the extra wake slot; raise Later when it is in use."""
        async with self._lock:
            if self.running >= self.max + 1:
                raise Later()
            self.running += 1

    def release(self) -> None:
        """Release one slot."""
        self.running = max(0, self.running - 1)

    @property
    def active_count(self) -> int:
        """Return the number of occupied slots."""
        return self.running

    @property
    def can_wake_extra(self) -> bool:
        """Return whether a human wake can still be admitted."""
        return self.running < self.max + 1


class ExecutorHost(Protocol):
    """Surface the Executor needs from the Runtime."""

    engine: Any
    effects: Any

    def settings(self) -> Any: ...


class SherryExecutor:
    """Launch, deliver to and stop worker subagents for Tickets."""

    def __init__(
        self,
        host: ExecutorHost,
        client: SherryClient,
        *,
        settings_getter: Callable[[], Any],
        deployment: Any = None,
        sources: Any = None,
    ) -> None:
        self.host = host
        self.client = client
        self.settings_getter = settings_getter
        self.deployment = deployment
        self.sources = sources
        self.slots = WorkerSlots(int(getattr(settings_getter(), "max_workers", 1)))
        self._held: dict[str, str] = {}
        self._attempts: dict[str, int] = {}

    def install(self) -> None:
        """Register effect handlers once the Runtime is assembled."""
        self.host.effects.handle("launch", self._handle_launch, retry=EFFECT_RETRY_LAUNCH)
        self.host.effects.handle("delivery", self._handle_delivery)
        self.host.effects.handle("cancel", self._handle_cancel)

    def start(self, ticket_id: str, assignment_id: str) -> None:
        """Enqueue a launch effect (executed after the Engine commit)."""
        self.host.effects.enqueue(
            "launch", ticket_id, {"ticket_id": ticket_id, "assignment_id": assignment_id}
        )

    def deliver(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        """Enqueue a delivery effect for pending Inputs."""
        self.host.effects.enqueue(
            "delivery",
            ticket_id,
            {"ticket_id": ticket_id, "assignment_id": assignment_id, "input_ids": input_ids},
        )

    def stop(self, ticket_id: str) -> None:
        """Cancel the worker subagent and release its slot."""
        run_id = self._held.pop(ticket_id, "")
        if run_id:
            self.host.effects.enqueue("cancel", ticket_id, {"run_id": run_id})
        self.slots.release()

    def prepare(self, ticket: Ticket, outcome: Any = None) -> dict[str, Any]:
        """Generate worker metadata; no commands are run here."""
        name = str(ticket.execution.get("name", ticket.id))
        root = getattr(self.deployment, "execution_root", "")
        directory = str(ticket.execution.get("directory") or os.path.join(str(root), name))
        ticket.execution.update({"name": name, "directory": directory})
        return {"name": name, "directory": directory, "outcome": outcome}

    async def _handle_cancel(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id", ""))
        if run_id:
            await self.client.post("/internal/superagent/cancel", {"run_id": run_id})

    async def _handle_launch(self, payload: dict[str, Any]) -> None:
        ticket_id = str(payload.get("ticket_id", ""))
        assignment_id = str(payload.get("assignment_id", ""))
        ticket = self.host.engine.get_ticket(ticket_id)
        if ticket is None:
            return
        await self.slots.acquire_launch()
        try:
            await self._prepare_directory(ticket)
            request = self._spawn_request(ticket)
            result = await self.client.post("/internal/superagent/spawn", request.as_dict())
            self._attempts.pop(ticket_id, None)
            self._held[ticket_id] = str(result.get("run_id", ""))
            self.host.engine.assignment_started(ticket_id, assignment_id)
        except Exception as exc:
            self.slots.release()
            if self._is_transient(exc):
                attempts = self._attempts.get(ticket_id, 0) + 1
                self._attempts[ticket_id] = attempts
                if attempts < TRANSIENT_RETRIES:
                    raise Later() from exc
            raise

    async def _handle_delivery(self, payload: dict[str, Any]) -> None:
        ticket_id = str(payload.get("ticket_id", ""))
        assignment_id = str(payload.get("assignment_id", ""))
        input_ids = [str(item) for item in payload.get("input_ids", [])]
        ticket = self.host.engine.get_ticket(ticket_id)
        if ticket is None:
            return
        run_id = self._held.get(ticket_id, "")
        try:
            await self.client.post(
                "/internal/superagent/session/add_messages",
                {"run_id": run_id, "messages": input_ids},
            )
            self.host.engine.inputs_delivered(ticket_id, assignment_id, input_ids)
        except Exception as exc:  # noqa: BLE001 — reported to the Engine as a delivery fact
            self.host.engine.delivery_failed(ticket_id, assignment_id, str(exc))

    def _spawn_request(self, ticket: Ticket) -> SpawnRequest:
        settings = self.settings_getter()
        getter = getattr(self.sources, "environment_values", None)
        raw = (
            getter(ticket.origin.get("source", "github"), ticket.origin, None)
            if callable(getter)
            else {}
        )
        variables = dict(raw) if isinstance(raw, dict) else {}
        env = execution_environment(ticket, variables=variables).as_dict()
        return SpawnRequest(
            step_model=str(getattr(settings, "executor_model", "")),
            cwd=str(ticket.execution.get("directory", "")),
            env=env,
            label=ticket.id,
        )

    async def _prepare_directory(self, ticket: Ticket) -> None:
        command = str(getattr(self.settings_getter(), "prepare", ""))
        directory = str(ticket.execution.get("directory", ""))
        if not command or os.path.isdir(directory):
            return
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=str(getattr(self.deployment, "execution_root", ".")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=PREPARE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            self.host.engine.report(
                ticket.id,
                Report(
                    assignment_id="",
                    kind=ReportKind.FAILED,
                    summary="prepare timed out after 600s",
                    at="",
                ),
            )
            raise RuntimeError("prepare timed out") from None
        if proc.returncode != 0:
            self.host.engine.report(
                ticket.id,
                Report(
                    assignment_id="",
                    kind=ReportKind.FAILED,
                    summary=f"prepare failed: {err.decode('utf-8', 'replace')[:500]}",
                    at="",
                ),
            )
            raise RuntimeError("prepare failed")

    def resume(self) -> list[str]:
        """Requeue interrupted assignments and pending launches, oldest first."""
        requeued: list[str] = []
        tickets = sorted(
            (ticket for ticket in self.host.engine.repo.tickets.list() if ticket.is_open),
            key=lambda item: item.origin.get("at", ""),
        )
        for ticket in tickets:
            if not ticket.assignments:
                continue
            latest = ticket.assignments[-1]
            status = str(latest.status)
            if status == "running":
                self.host.engine.assignment_interrupted(ticket.id, latest.id)
                requeued.append(ticket.id)
            elif status == "pending" and not ticket.has_undelivered_input:
                self.start(ticket.id, latest.id)
                requeued.append(ticket.id)
        return requeued

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        if isinstance(exc, SherryHTTPError):
            return exc.transient
        return isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError))


__all__ = [
    "AiohttpSherryClient",
    "ExecutorHost",
    "SherryClient",
    "SherryExecutor",
    "SherryHTTPError",
    "SpawnRequest",
    "TRANSIENT_CODES",
    "WorkerSlots",
]
