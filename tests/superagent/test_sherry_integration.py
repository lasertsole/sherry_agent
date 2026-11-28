"""Sherry loopback endpoints and the Sessions client seam."""

from __future__ import annotations

import pytest

# Aliased: a bare TestClient name makes pytest try to collect the class.
from robyn.testing import TestClient as _TestClient

from server.trigger.auth import TOKEN_HEADER, gateway_token
from server.trigger.core import app
from server.trigger.http import superagent as loopback
from superagent.app.sessions import MAIN_SESSION_KEY, Sessions

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]
client = _TestClient(app)


@pytest.fixture(autouse=True)
def clean_hooks():
    loopback.clear_hooks()
    loopback.set_events_url("")
    yield
    loopback.clear_hooks()
    loopback.set_events_url("")


def headers() -> dict[str, str]:
    return {TOKEN_HEADER: gateway_token()}


def test_spawn_endpoint_dispatches_registered_hook() -> None:
    seen = []
    loopback.register_hook("spawn", lambda payload: seen.append(payload) or {"run_id": "run-9"})
    response = client.post(
        "/internal/superagent/spawn",
        json_data={"requester_session_key": MAIN_SESSION_KEY, "functional_role": "executor"},
        headers=headers(),
    )
    assert response.status_code == 200
    assert response.json()["run_id"] == "run-9"
    assert seen[0]["functional_role"] == "executor"


def test_session_endpoints_cover_all_loopback_operations() -> None:
    seen: list[str] = []

    def record(name):
        def handler(payload):
            seen.append(name)
            return {"key": payload.get("key", ""), "jobs": [{"id": "j1"}]}

        return handler

    for name in (
        "session.ensure",
        "session.heal",
        "session.inject",
        "session.add_messages",
        "session.truncate",
        "session.kill_job",
    ):
        loopback.register_hook(name, record(name))
    for name in ("session.exists", "session.state", "session.jobs", "session.trajectory"):
        loopback.register_hook(name, record(name))

    loopback.register_hook("session.ensure", record("session.ensure"))
    assert (
        client.post(
            "/internal/superagent/session/ensure", json_data={}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/superagent/session/exists",
            json_data={"key": "superagent-main"},
            headers=headers(),
        ).json()["handled"]
        is True
    )
    assert (
        client.post(
            "/internal/superagent/session/inject", json_data={"text": "go"}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/superagent/session/heal", json_data={}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/superagent/session/add_messages", json_data={}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/superagent/session/truncate", json_data={}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/superagent/session/state", json_data={"key": "x"}, headers=headers()
        ).status_code
        == 200
    )
    assert client.post(
        "/internal/superagent/session/jobs", json_data={"key": "x"}, headers=headers()
    ).json()["jobs"] == [{"id": "j1"}]
    assert (
        client.post(
            "/internal/superagent/session/kill_job", json_data={"job_id": "j1"}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/superagent/session/trajectory", json_data={"key": "x"}, headers=headers()
        ).status_code
        == 200
    )
    assert "session.ensure" in seen and "session.trajectory" in seen


def test_bad_body_is_rejected() -> None:
    response = client.post("/internal/superagent/spawn", json_data=None, headers=headers())
    assert response.status_code in (400, 422)


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | dict]] = []

    async def post(self, path: str, payload: dict) -> dict:
        self.calls.append(("post", path))
        if "exists" in path:
            return {"exists": True}
        return {"ok": True, "jobs": [{"id": "j1"}]}

    async def get(self, path: str) -> dict:
        self.calls.append(("get", path))
        if "jobs" in path:
            return {"jobs": [{"id": "j1"}]}
        if "trajectory" in path:
            return {"events": [{"kind": "completion"}]}
        return {"exists": True, "state": {}}


@pytest.mark.asyncio
async def test_sessions_client_wires_every_operation() -> None:
    client = Client()
    sessions = Sessions(client)
    assert (await sessions.ensure())["ok"] is True
    assert await sessions.exists() is True
    assert (await sessions.inject("candidate"))["ok"] is True
    assert (await sessions.heal("worker-1"))["ok"] is True
    assert (await sessions.add_messages("worker-1", ["m1"]))["ok"] is True
    assert (await sessions.truncate("worker-1", 3))["ok"] is True
    assert await sessions.read_state("worker-1") == {"exists": True, "state": {}}
    assert await sessions.list_jobs("worker-1") == [{"id": "j1"}]
    assert (await sessions.kill_job("worker-1", "j1"))["ok"] is True
    assert await sessions.read_trajectory("worker-1") == [{"kind": "completion"}]
    assert all(str(path).startswith("/internal/superagent/") for _method, path in client.calls)
    assert sessions.degraded is False
