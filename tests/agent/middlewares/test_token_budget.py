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


class TestCacheAwareCost:
    """Anthropic cache reads/writes are priced at their own rates (§6).

    The estimate used to charge every input token at the plain input rate and
    ignore the cache counters entirely, so a cached turn looked exactly as
    expensive as an uncached one — the whole point of caching is the discount.
    """

    @pytest.fixture(autouse=True)
    def _prices(self, monkeypatch: pytest.MonkeyPatch):
        from config.features.infra_side import model_pricing

        table = {
            "claude-sonnet-4": {
                "input": 3.0,
                "output": 15.0,
                "cache_read": 0.3,
                "cache_write": 3.75,
            },
            "_default": {"input": 0.5, "output": 1.5},
        }
        monkeypatch.setitem(model_pricing.MODEL_PRICING, "model_pricing_per_m_tokens", table)
        return table

    def test_a_cache_read_is_charged_the_discount_rate(self):
        message = _ai(
            input_tokens=1000,
            output_tokens=100,
            model="claude-sonnet-4",
            usage_extra={"input_token_details": {"cache_read": 9000}},
        )

        cost = budget_core._cost_delta(message, message.usage_metadata, 1000, 100)

        # 1000 uncached @3 + 9000 cached @0.3 + 100 out @15
        assert cost == pytest.approx((1000 * 3.0 + 9000 * 0.3 + 100 * 15.0) / 1_000_000)

    def test_a_cache_write_is_charged_the_premium_rate(self):
        message = _ai(
            input_tokens=0,
            output_tokens=0,
            model="claude-sonnet-4",
            usage_extra={"input_token_details": {"cache_creation": 10000}},
        )

        cost = budget_core._cost_delta(message, message.usage_metadata, 0, 0)

        assert cost == pytest.approx(10000 * 3.75 / 1_000_000)

    def test_a_cached_turn_costs_less_than_the_same_tokens_as_fresh_input(self):
        cached = _ai(
            input_tokens=1000,
            output_tokens=100,
            model="claude-sonnet-4",
            usage_extra={"input_token_details": {"cache_read": 9000}},
        )
        all_fresh = _ai(input_tokens=10000, output_tokens=100, model="claude-sonnet-4")

        cached_cost = budget_core._cost_delta(cached, cached.usage_metadata, 1000, 100)
        fresh_cost = budget_core._cost_delta(all_fresh, all_fresh.usage_metadata, 10000, 100)

        assert cached_cost < fresh_cost

    def test_a_table_without_cache_prices_keeps_the_old_estimate(self, monkeypatch):
        from config.features.infra_side import model_pricing

        monkeypatch.setitem(
            model_pricing.MODEL_PRICING,
            "model_pricing_per_m_tokens",
            {
                "claude-sonnet-4": {"input": 3.0, "output": 15.0},
                "_default": {"input": 0.5, "output": 1.5},
            },
        )
        message = _ai(
            input_tokens=1000,
            output_tokens=0,
            model="claude-sonnet-4",
            usage_extra={"input_token_details": {"cache_read": 9000}},
        )

        cost = budget_core._cost_delta(message, message.usage_metadata, 1000, 0)

        # Cached tokens fall back to the input rate: no invented discount.
        assert cost == pytest.approx((1000 * 3.0 + 9000 * 3.0) / 1_000_000)

    def test_an_openai_shaped_usage_subtracts_the_cached_subset(self):
        """OpenAI's ``input_tokens`` CONTAINS the cached ones; Anthropic's does not."""
        message = _ai(
            input_tokens=10000,
            output_tokens=0,
            model="claude-sonnet-4",
            usage_extra={"input_token_details": {"cache_read": 9000}},
        )
        message.response_metadata["usage"] = {"prompt_tokens": 10000, "cached_tokens": 9000}

        cost = budget_core._cost_delta(message, message.usage_metadata, 10000, 0)

        # 1000 uncached @3 + 9000 cached @0.3 — not 10000 uncached.
        assert cost == pytest.approx((1000 * 3.0 + 9000 * 0.3) / 1_000_000)

    @pytest.mark.asyncio
    async def test_the_middleware_accumulates_the_discounted_cost(self, registers, events):
        mem, _db = registers

        async def handler(_request):
            return _ai(
                input_tokens=1000,
                output_tokens=0,
                model="claude-sonnet-4",
                usage_extra={"input_token_details": {"cache_read": 9000}},
            )

        await TokenBudget().awrap_model_call(_request(), handler)

        assert mem.get_state(SESSION, StateKey.TOKEN_BUDGET_COST) == pytest.approx(
            (1000 * 3.0 + 9000 * 0.3) / 1_000_000
        )
