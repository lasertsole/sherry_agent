"""Per-session token budget settings: park/promote, state, reset."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from runtime import StateKey
from server.service import session_settings_service as service

pytestmark = [pytest.mark.unit]

SESSION = "sess-budget-settings"


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


class _FakeQueue:
    async def list_active(self, _session_key: str) -> list:
        return []


@asynccontextmanager
async def _null_lock(_session_id: str):
    yield


@pytest.fixture()
def registers(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeRegister, _FakeRegister]:
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(service, "state_register_mem", mem)
    monkeypatch.setattr(service, "state_register_db", db)
    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: False)
    monkeypatch.setattr(
        "server.service.input_queue_service._get_session_lock", lambda _sid: _null_lock(_sid)
    )
    monkeypatch.setattr(
        "server.service.input_queue_service.get_default_queue", lambda: _FakeQueue()
    )
    return mem, db


@pytest.mark.asyncio
async def test_apply_idle_writes_the_live_cap(registers):
    mem, _db = registers

    pending = await service.apply_token_budget_choice(SESSION, 5000)

    assert pending is False
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) == 5000
    assert service.get_token_budget_state(SESSION)["cap"] == 5000


@pytest.mark.asyncio
async def test_apply_busy_parks_then_promotes(registers, monkeypatch: pytest.MonkeyPatch):
    mem, _db = registers

    def _busy(_sid: str) -> bool:
        return True

    monkeypatch.setattr(service, "_session_turn_active", _busy)
    pending = await service.apply_token_budget_choice(SESSION, 9000)

    assert pending is True
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) is None
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP_PENDING) == {"value": 9000}

    promoted = service.promote_pending_settings_sync(SESSION)

    assert str(StateKey.TOKEN_BUDGET_CAP) in promoted
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) == 9000
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP_PENDING) is None


@pytest.mark.asyncio
async def test_a_promoted_cap_clears_the_exhausted_latch(registers, monkeypatch):
    """Raising the cap mid-turn must land with a fresh latch at promotion."""
    mem, _db = registers
    await service.apply_token_budget_choice(SESSION, 1000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_USED, 1000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_LIMITED, True)
    monkeypatch.setattr(service, "_session_turn_active", lambda _sid: True)

    await service.apply_token_budget_choice(SESSION, 5000)
    service.promote_pending_settings_sync(SESSION)

    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) == 5000
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_LIMITED) is None
    assert service.get_token_budget_state(SESSION)["limited"] is False


@pytest.mark.asyncio
async def test_reset_clears_usage_but_keeps_the_cap(registers):
    mem, _db = registers
    await service.apply_token_budget_choice(SESSION, 5000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_USED, 2000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_COST, 0.25)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_LIMITED, True)

    state = service.reset_token_budget(SESSION)

    assert state == {
        "cap": 5000,
        "session_cap": 5000,
        "default_cap": 0,
        "inherited": False,
        "used": 0,
        "cost": 0.0,
        "limited": False,
        "pending": False,
    }
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) == 5000


@pytest.mark.asyncio
async def test_clearing_the_cap_clears_the_latched_flag(registers):
    mem, _db = registers
    await service.apply_token_budget_choice(SESSION, 1000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_LIMITED, True)

    pending = await service.apply_token_budget_choice(SESSION, None)

    assert pending is False
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_CAP) is None
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_LIMITED) is None


@pytest.mark.asyncio
async def test_state_reports_inheritance_and_the_app_default(
    registers, monkeypatch: pytest.MonkeyPatch
):
    # The service holds its own reference to the helper (module-scope import).
    monkeypatch.setattr(service, "default_token_cap", lambda: 9000)

    state = service.get_token_budget_state(SESSION)
    assert state["inherited"] is True
    assert state["session_cap"] is None
    assert state["default_cap"] == 9000
    assert state["cap"] == 9000, "the inherited cap is what gets enforced"

    # A session's own cap wins…
    await service.apply_token_budget_choice(SESSION, 4000)
    own = service.get_token_budget_state(SESSION)
    assert own["inherited"] is False and own["session_cap"] == 4000 and own["cap"] == 4000
    assert own["default_cap"] == 9000

    # …and an explicit 0 is "unlimited", not "inherit".
    await service.apply_token_budget_choice(SESSION, 0)
    unlimited = service.get_token_budget_state(SESSION)
    assert unlimited["inherited"] is False and unlimited["session_cap"] == 0
    assert unlimited["cap"] is None

    # Clearing the choice goes back to inheriting the default.
    await service.apply_token_budget_choice(SESSION, None)
    inherited_again = service.get_token_budget_state(SESSION)
    assert inherited_again["inherited"] is True and inherited_again["cap"] == 9000
