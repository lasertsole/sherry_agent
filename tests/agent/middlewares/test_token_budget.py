"""Session token budget middleware: short-circuit, accumulation, events."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent.middlewares.token_budget import core as budget_core
from agent.middlewares.token_budget.core import (
    BUDGET_LIMITED_EVENT,
    BUDGET_WARNING_EVENT,
    TokenBudget,
)
from runtime import StateKey

pytestmark = [pytest.mark.unit]

SESSION = "sess-token-budget"


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


@pytest.fixture()
def registers(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeRegister, _FakeRegister]:
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(budget_core, "state_register_mem", mem)
    monkeypatch.setattr(budget_core, "state_register_db", db)
    budget_core.reset_budget_notice(SESSION)
    return mem, db


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        budget_core,
        "_notify",
        lambda sid, event, content: seen.append((sid, event, content)),
    )
    return seen


def _request() -> Any:
    return SimpleNamespace(state={"session_id": SESSION})


def _ai(
    *,
    input_tokens: int,
    output_tokens: int,
    model: str = "deepseek-chat",
    usage_extra: dict | None = None,
) -> AIMessage:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    usage.update(usage_extra or {})
    return AIMessage(
        content="ok",
        usage_metadata=usage,
        response_metadata={"model_name": model},
    )


@pytest.mark.asyncio
async def test_no_cap_passes_through_and_accumulates(registers, events):
    mem, _db = registers
    called: list[bool] = []

    async def handler(_request):
        called.append(True)
        return _ai(input_tokens=100, output_tokens=50)

    result = await TokenBudget().awrap_model_call(_request(), handler)

    assert isinstance(result, AIMessage) and called == [True]
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_USED) == 150
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_COST) == pytest.approx(
        (100 * 0.14 + 50 * 0.28) / 1_000_000
    )
    assert events == []


@pytest.mark.asyncio
async def test_exhausted_cap_short_circuits_without_calling_the_model(registers, events):
    mem, _db = registers
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_CAP, 1000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_USED, 1000)
    called: list[bool] = []

    async def handler(_request):
        called.append(True)
        return _ai(input_tokens=1, output_tokens=1)

    result = await TokenBudget().awrap_model_call(_request(), handler)

    assert isinstance(result, AIMessage)
    assert "Token budget exhausted" in result.content
    assert called == []
    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_LIMITED) is True
    assert events and events[0][1] == BUDGET_LIMITED_EVENT


@pytest.mark.asyncio
async def test_provider_cost_is_used_verbatim(registers, events):
    mem, _db = registers

    async def handler(_request):
        return _ai(input_tokens=10, output_tokens=20, usage_extra={"cost": 0.0123})

    await TokenBudget().awrap_model_call(_request(), handler)

    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_COST) == pytest.approx(0.0123)


@pytest.mark.asyncio
async def test_provider_prompt_plus_completion_cost_is_summed(registers, events):
    mem, _db = registers

    async def handler(_request):
        return _ai(
            input_tokens=10,
            output_tokens=20,
            usage_extra={"prompt_cost": 0.005, "completion_cost": 0.003},
        )

    await TokenBudget().awrap_model_call(_request(), handler)

    assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_COST) == pytest.approx(0.008)


@pytest.mark.asyncio
async def test_warning_fires_once_at_threshold(registers, events):
    mem, _db = registers
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_CAP, 1000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_USED, 750)

    async def handler(_request):
        return _ai(input_tokens=50, output_tokens=0)

    await TokenBudget().awrap_model_call(_request(), handler)
    await TokenBudget().awrap_model_call(_request(), handler)

    warnings = [event for event in events if event[1] == BUDGET_WARNING_EVENT]
    assert len(warnings) == 1
    assert warnings[0][2]["used"] == 800
    assert warnings[0][2]["cap"] == 1000


@pytest.mark.asyncio
async def test_limited_latch_fires_once(registers, events):
    mem, _db = registers
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_CAP, 1000)
    mem.set_state(SESSION, StateKey.TOKEN_BUDGET_USED, 950)

    async def handler(_request):
        return _ai(input_tokens=100, output_tokens=0)

    await TokenBudget().awrap_model_call(_request(), handler)
    await TokenBudget().awrap_model_call(_request(), handler)

    limited = [event for event in events if event[1] == BUDGET_LIMITED_EVENT]
    assert len(limited) == 1
