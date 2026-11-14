"""End-to-end extraction-settings path: HTTP controls + gates.

Walks the operator path in one process: the global endpoint reports the shipped
defaults, a session PUT overrides experience learning + the main ceiling (the
child ceiling is clamped), flow replay compilation is refused at the session
scope, a mid-turn PUT parks until the turn boundary, and IterationBudget reads
the effective value through its per-session resolver.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

import runtime.session.extraction as extraction
from agent.middlewares.iteration_budget.core import IterationBudget
from runtime import StateKey
from server.service import session_settings_service as service
from server.service import extraction_settings_service as extraction_service
from server.trigger.http import extraction as extraction_http

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

SESSION = "sess-extraction-api"


class _FakeRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self.data[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.data.pop((session_id, key), None)
        return True


class _FakeRequest:
    def __init__(self, query_params: dict | None = None, body: dict | None = None):
        self.query_params = query_params or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeQueue:
    async def list_active(self, _session_key: str) -> list:
        return []


@asynccontextmanager
async def _null_lock(_session_id: str):
    yield


@pytest.fixture()
def registers(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeRegister, _FakeRegister]:
    """One register pair shared by the HTTP service, the runtime reader and the middleware."""
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(service, "state_register_mem", mem)
    monkeypatch.setattr(service, "state_register_db", db)
    monkeypatch.setattr(extraction, "state_register_mem", mem)
    monkeypatch.setattr(extraction, "state_register_db", db)
    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: False)
    monkeypatch.setattr(
        "server.service.input_queue_service._get_session_lock", lambda _sid: _null_lock(_sid)
    )
    monkeypatch.setattr(
        "server.service.input_queue_service.get_default_queue", lambda: _FakeQueue()
    )
    # The shipped defaults for the global scope (no sherry.jsonc read).
    monkeypatch.setattr(
        extraction,
        "load_sherry_settings",
        lambda: {
            "extraction.skill_extraction_enabled": True,
            "extraction.flow_extraction_enabled": True,
            "extraction.iteration_budget_main": 180,
            "extraction.iteration_budget_sub": 90,
        },
    )
    return mem, db


def test_global_endpoint_reports_the_shipped_defaults(registers) -> None:
    response = asyncio.run(extraction_http.get_global_extraction_handler(_FakeRequest()))
    assert response.status_code == 200, response.description
    assert '"skill_extraction_enabled": true' in response.description
    assert '"flow_extraction_enabled": true' in response.description
    assert '"iteration_budget_main": 180' in response.description
    assert '"iteration_budget_sub": 90' in response.description


def test_session_override_lands_live_and_is_effective(registers) -> None:
    mem, _db = registers
    put = asyncio.run(
        extraction_http.put_session_extraction_handler(
            _FakeRequest(
                body={
                    "session_id": SESSION,
                    "overrides": {
                        "skill_extraction_enabled": False,
                        "iteration_budget_main": 100,
                        "iteration_budget_sub": 200,
                    },
                }
            )
        )
    )
    assert put.status_code == 200, put.description
    assert '"pending": true' not in put.description
    stored = mem.get_state(SESSION, StateKey.EXTRACTION)
    assert stored["skill_extraction_enabled"] is False
    assert stored["iteration_budget_main"] == 100
    # The child ceiling is clamped to the main one.
    assert stored["iteration_budget_sub"] == 100
    # Parent off forces flow off in the effective view.
    assert extraction.effective_extraction_config(SESSION)["flow_extraction_enabled"] is False

    get = asyncio.run(
        extraction_http.get_session_extraction_handler(
            _FakeRequest(query_params={"session_id": SESSION})
        )
    )
    assert get.status_code == 200, get.description
    assert '"skill_extraction_enabled": false' in get.description
    assert '"flow_extraction_enabled": false' in get.description


def test_a_session_cannot_override_flow_compilation(registers) -> None:
    response = asyncio.run(
        extraction_http.put_session_extraction_handler(
            _FakeRequest(
                body={"session_id": SESSION, "overrides": {"flow_extraction_enabled": False}}
            )
        )
    )
    assert response.status_code == 400
    assert "global setting" in response.description


def test_a_mid_turn_choice_parks_until_the_turn_boundary(registers, monkeypatch) -> None:
    mem, _db = registers
    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: True)

    parked = asyncio.run(
        extraction_http.put_session_extraction_handler(
            _FakeRequest(
                body={
                    "session_id": SESSION,
                    "overrides": {"skill_extraction_enabled": False},
                }
            )
        )
    )
    assert parked.status_code == 200
    assert '"pending": true' in parked.description
    # Nothing reached the live key; the running turn keeps its values.
    assert mem.get_state(SESSION, StateKey.EXTRACTION) is None
    assert mem.get_state(SESSION, StateKey.EXTRACTION_PENDING) == {
        "value": {"skill_extraction_enabled": False}
    }

    # The gate still sees the OLD (global) value until promotion.
    assert extraction.effective_extraction_config(SESSION)["skill_extraction_enabled"] is True

    promoted = service.promote_pending_settings_sync(SESSION)
    assert str(StateKey.EXTRACTION) in promoted
    assert mem.get_state(SESSION, StateKey.EXTRACTION) == {"skill_extraction_enabled": False}
    assert mem.get_state(SESSION, StateKey.EXTRACTION_PENDING) is None
    assert extraction.effective_extraction_config(SESSION)["skill_extraction_enabled"] is False


def test_clearing_overrides_returns_field_by_field_to_global(registers) -> None:
    mem, _db = registers
    mem.set_state(SESSION, StateKey.EXTRACTION, {"iteration_budget_main": 20})

    cleared = asyncio.run(
        extraction_http.put_session_extraction_handler(
            _FakeRequest(body={"session_id": SESSION, "overrides": {}})
        )
    )
    assert cleared.status_code == 200
    assert mem.get_state(SESSION, StateKey.EXTRACTION) is None
    assert extraction.effective_extraction_config(SESSION)["iteration_budget_main"] == 180


def test_global_write_goes_through_the_typed_config_writer(registers, monkeypatch) -> None:
    recorded: dict[str, Any] = {}

    def _write(changes: dict[str, Any]) -> None:
        recorded.update(changes)

    monkeypatch.setattr("server.service.sherry_config.write_sherry_config", _write)
    response = asyncio.run(
        extraction_http.put_global_extraction_handler(
            _FakeRequest(
                body={
                    "values": {
                        "skill_extraction_enabled": False,
                        "flow_extraction_enabled": True,
                        "iteration_budget_main": 120,
                        "iteration_budget_sub": 60,
                    }
                }
            )
        )
    )
    assert response.status_code == 200, response.description
    assert recorded == {
        "extraction.skill_extraction_enabled": False,
        "extraction.flow_extraction_enabled": True,
        "extraction.iteration_budget_main": 120,
        "extraction.iteration_budget_sub": 60,
    }


def test_iteration_budget_reads_the_session_ceiling(registers, monkeypatch) -> None:
    mem, _db = registers
    mem.set_state(SESSION, StateKey.EXTRACTION, {"iteration_budget_main": 2})
    budget = IterationBudget(
        max_iterations=180,
        cap_resolver=lambda sid: extraction.effective_extraction_config(sid)[
            "iteration_budget_main"
        ],
    )
    import agent.middlewares.iteration_budget.core as budget_core

    monkeypatch.setattr(budget_core, "state_register_mem", mem)
    budget._before_agent_impl({"session_id": SESSION})
    assert mem.get_state(SESSION, StateKey.ITERATION_BUDGET) == 2
    assert budget._consume(SESSION) is True
    assert budget._consume(SESSION) is True
    assert budget._consume(SESSION) is False


def test_the_service_state_marks_a_parked_choice(registers, monkeypatch) -> None:
    mem, _db = registers
    mem.set_state(SESSION, StateKey.EXTRACTION_PENDING, {"value": {"iteration_budget_main": 33}})
    state = extraction_service.get_extraction_state(SESSION)
    assert state["pending"] is True
    assert state["session"] == {"iteration_budget_main": 33}
    assert state["effective"]["iteration_budget_main"] == 33
