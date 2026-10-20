"""End-to-end token budget path: HTTP controls + middleware enforcement.

The unit suites pin the middleware and the service separately. This test walks
the whole operator path in one process — ``PUT /sessions/budget`` writes the
cap through the service's real park/promote path, the middleware consumes and
accumulates provider usage on the same registers, ``GET /sessions/budget``
reports it, and ``POST /sessions/budget/reset`` makes a stopped session usable
again without losing the cap.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent.middlewares.token_budget import core as budget_core
from agent.middlewares.token_budget.core import TokenBudget
from runtime import StateKey
from server.service import session_settings_service as service
from server.trigger.http import session_settings as session_settings_http

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

SESSION = "sess-budget-e2e"


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
    """One register pair shared by the HTTP service and the middleware."""
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(service, "state_register_mem", mem)
    monkeypatch.setattr(service, "state_register_db", db)
    monkeypatch.setattr(budget_core, "state_register_mem", mem)
    monkeypatch.setattr(budget_core, "state_register_db", db)
    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: False)
    monkeypatch.setattr(
        "server.service.input_queue_service._get_session_lock", lambda _sid: _null_lock(_sid)
    )
    monkeypatch.setattr(
        "server.service.input_queue_service.get_default_queue", lambda: _FakeQueue()
    )
    budget_core.reset_budget_notice(SESSION)
    return mem, db


def _request() -> Any:
    return SimpleNamespace(state={"session_id": SESSION})


def _ai(input_tokens: int, output_tokens: int) -> AIMessage:
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost": 0.01,
        },
        response_metadata={"model_name": "deepseek-chat"},
    )


async def _call(tokens: tuple[int, int], calls: list[tuple[int, int]]) -> Any:
    async def handler(_request):
        calls.append(tokens)
        return _ai(*tokens)

    return await TokenBudget().awrap_model_call(_request(), handler)


def test_http_cap_middleware_accumulation_and_reset(registers):
    mem, _db = registers

    put = asyncio.run(
        session_settings_http.put_budget_handler(
            _FakeRequest(body={"session_id": SESSION, "cap": 300})
        )
    )
    assert put.status_code == 200, put.description
    assert '"pending": false' in put.description
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) == 300

    calls: list[tuple[int, int]] = []
    asyncio.run(_call((100, 50), calls))
    asyncio.run(_call((100, 100), calls))

    after = asyncio.run(
        session_settings_http.get_budget_handler(_FakeRequest(query_params={"session_id": SESSION}))
    )
    assert after.status_code == 200, after.description
    assert '"used": 350' in after.description
    assert '"cost": 0.02' in after.description
    assert '"limited": true' in after.description

    # The next model call is short-circuited: the handler never runs.
    stopped = asyncio.run(_call((10, 10), calls))
    assert isinstance(stopped, AIMessage) and "Token budget exhausted" in stopped.content
    assert calls == [(100, 50), (100, 100)]

    reset = asyncio.run(
        session_settings_http.reset_budget_handler(_FakeRequest(body={"session_id": SESSION}))
    )
    assert reset.status_code == 200, reset.description
    assert '"cap": 300' in reset.description
    assert '"used": 0' in reset.description
    assert '"limited": false' in reset.description

    # Reset clears usage and the latch but the cap still applies afterwards.
    asyncio.run(_call((10, 10), calls))
    assert calls[-1] == (10, 10)
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_USED) == 20

    cleared = asyncio.run(
        session_settings_http.put_budget_handler(
            _FakeRequest(body={"session_id": SESSION, "cap": None})
        )
    )
    assert cleared.status_code == 200, cleared.description
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) is None


def test_a_parked_cap_applies_on_the_next_turn(registers, monkeypatch: pytest.MonkeyPatch):
    """Mid-turn PUT parks; the running turn keeps the old cap, then promotion wins."""
    mem, _db = registers
    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: True)

    parked = asyncio.run(
        session_settings_http.put_budget_handler(
            _FakeRequest(body={"session_id": SESSION, "cap": 500})
        )
    )
    assert parked.status_code == 200
    assert '"pending": true' in parked.description
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) is None

    seen = asyncio.run(
        session_settings_http.get_budget_handler(_FakeRequest(query_params={"session_id": SESSION}))
    )
    assert '"cap": 500' in seen.description and '"pending": true' in seen.description

    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: False)
    service.promote_pending_settings_sync(SESSION)
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) == 500
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP_PENDING) is None
