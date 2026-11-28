"""Panel HTTP API guard and route tests."""

from __future__ import annotations

import json

import pytest

from superagent.app.prompts import Prompts
from superagent.app.settings import SettingsStore
from superagent.core.memory import MemoryRepositories
from superagent.infra.commands import FakeCommands
from superagent.infra.health import HealthBoard
from superagent.panel.api import PanelAPI
from superagent.panel.http import PanelHTTP
from superagent.sources.registry import SourceRegistry
from tests.superagent.conftest import make_ticket, make_trigger


class Engine:
    def __init__(self) -> None:
        self.repo = MemoryRepositories()
        self.repo.triggers.save(make_trigger())
        ticket = make_ticket()
        self.repo.tickets.save(ticket)
        self.changed = 0
        self.actions: list[str] = []

    def policy_changed(self, old, new) -> None:
        self.changed += 1

    def retry_ticket(self, ticket_id: str) -> None:
        self.actions.append(f"retry:{ticket_id}")

    def stop_ticket(self, ticket_id: str) -> None:
        self.actions.append(f"stop:{ticket_id}")


def build():
    engine = Engine()
    settings = SettingsStore()
    prompts = Prompts()
    registry = SourceRegistry(FakeCommands(), HealthBoard())
    api = PanelAPI(
        engine=engine,
        settings=settings,
        prompts=prompts,
        board=HealthBoard(),
        registry=registry,
    )
    return api, engine, settings


GOOD = {"x-orchestrator-request": "1"}


def test_guard_rejects_missing_header_origin_and_host() -> None:
    api, _engine, _settings = build()
    assert api.handle("GET", "/state")[0] == 403
    assert api.handle("GET", "/state", headers=GOOD, origin="https://evil.test")[0] == 403
    assert api.handle("GET", "/state", headers=GOOD, host="evil.test:3082")[0] == 403
    assert api.handle("GET", "/state", headers=GOOD, host="127.0.0.1:3082")[0] == 200


def test_state_settings_sources_and_prompts_routes() -> None:
    api, _engine, _settings = build()
    status, state = api.handle("GET", "/state", headers=GOOD, host="localhost:3082")
    assert status == 200 and state["masterId"] == "superagent-main"
    status, settings = api.handle("GET", "/settings", headers=GOOD)
    assert status == 200 and "groups" in settings
    status, sources = api.handle("GET", "/sources", headers=GOOD)
    assert status == 200 and sources["sources"] == []
    status, prompts = api.handle("GET", "/prompts", headers=GOOD)
    assert status == 200 and prompts["revision"] == 0


def test_save_settings_stale_revision_and_policy_change() -> None:
    api, engine, settings = build()
    status, result = api.handle(
        "PUT",
        "/settings/policy",
        body={"value": {"accept_mode": "auto"}, "revision": 0},
        headers=GOOD,
    )
    assert status == 200 and result["revision"] == 1
    assert engine.changed == 1
    status, result = api.handle(
        "PUT", "/settings/policy", body={"value": {}, "revision": 0}, headers=GOOD
    )
    assert status == 409 and result["code"] == "stale_revision"


def test_prompts_and_ticket_actions() -> None:
    api, engine, _settings = build()
    status, result = api.handle(
        "PUT", "/prompts", body={"config": {"skills": ["worker"]}, "revision": 0}, headers=GOOD
    )
    assert status == 200 and result["revision"] == 1
    assert api.handle("PUT", "/prompts", body={"revision": 0}, headers=GOOD)[0] == 409
    assert api.handle("POST", "/tickets/t1/retry", headers=GOOD)[0] == 200
    assert api.handle("POST", "/tickets/t1/stop", headers=GOOD)[0] == 200
    assert engine.actions == ["retry:t1", "stop:t1"]
    assert api.handle("GET", "/unknown", headers=GOOD)[0] == 404


@pytest.mark.asyncio
async def test_http_adapter_validates_json_body() -> None:
    api, _engine, _settings = build()
    http = PanelHTTP(api)
    status, _body = await http.serve("POST", "/executor/pause", raw_body="not-json", headers=GOOD)
    assert status == 400
    status, body = await http.serve(
        "POST",
        "/executor/pause",
        raw_body=json.dumps({"paused": True}).encode(),
        headers=GOOD,
    )
    assert status == 200 and body["paused"] is True
    status, _body = await http.serve(
        "POST", "/executor/pause", raw_body=b"x" * 1_100_000, headers=GOOD
    )
    assert status == 400
