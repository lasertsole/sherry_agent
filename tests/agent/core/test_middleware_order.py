"""Order contract for the middleware pipelines assembled by the agent builders.

The middleware list order is load-bearing: LangChain executes ``after_model``
nodes in reverse registration order and ``wrap_model_call`` layers
outermost-first. This pins the exact sequence produced by
``agent.core._build_middlewares`` through the real ``built_agent`` path, and
the child sequence produced by
``agent/tools/subagent/spawn/core.py::_build_child_middlewares`` (whose
docstring defers to "the subagent side of the middleware-order tests" — this
is that side), so a builder refactor cannot silently reorder hooks.
"""

from typing import Any

import pytest

from agent import core as agent_core
from agent.tools.subagent.spawn.core import _build_child_middlewares
from config.features import DELEGATION_PRESSURE

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

EXPECTED_ORDER = [
    "PlanContinuationEnforcer",
    "system_prompt_injection",
    "WorkspaceNoticeMiddleware",
    "PlanModeToolGate",
    "MultimodalProcessor",
    "IterationBudget",
    "TokenBudget",
    "ToolResultEnrichmentMiddleware",
    "ToolGuardrails",
    "ToolResourceLockMiddleware",
    "MediaBudgetProjectionMiddleware",
    "UserHooksMiddleware",
    "ContextEvictionMiddleware",
    "ToolCallNormalize",
    "PathGuard",
    "SubagentCompletionDrainMiddleware",
    "TaskIntentMiddleware",
    "DynamicReminderMiddleware",
    "InputSafetyFilter",
    "LLMModerationMiddleware",
    "OutputRepetitionGuard",
    "MaxTokensBoostMiddleware",
    "ThinkingControlMiddleware",
    "HeartbeatStaleness",
    "HumanInTheLoop",
    "MessagePersistenceMiddleware",
    "LLMRetryMiddleware",
    "OutputSafetyFilter",
    "Summarization",
    "InvalidToolCallRepairMiddleware",
    "DelegationPressureGate",
]

#: Child-chain order (``_build_child_middlewares``). Leaner by design — no
#: HITL / eviction / persistence / path guard / LLM retry / task layer (see the
#: builder's docstring) — and ``InvalidToolCallRepairMiddleware`` is LAST there
#: too, mirroring the main chain's innermost-wrap position.
SUBAGENT_EXPECTED_ORDER = [
    "Summarization",
    "IterationBudget",
    "ChildTokenBudget",
    "ToolGuardrails",
    "ToolResourceLockMiddleware",
    "MediaBudgetProjectionMiddleware",
    "OutputRepetitionGuard",
    "MaxTokensBoostMiddleware",
    "ToolCallNormalize",
    "HeartbeatStaleness",
    "InvalidToolCallRepairMiddleware",
]


def test_subagent_middleware_order_is_pinned() -> None:
    chain = _build_child_middlewares(auxiliary_llm=None, main_llm_context_window=131_072)
    assert [type(mw).__name__ for mw in chain] == SUBAGENT_EXPECTED_ORDER
    # The delegation gate is a MAIN-agent-only middleware; a child must never
    # inherit it even though its config entry is enabled.
    assert "DelegationPressureGate" not in [type(mw).__name__ for mw in chain]


def test_delegation_pressure_gate_is_conditionally_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spec's ``when`` predicate controls the gate's presence, not position."""
    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "0")
    monkeypatch.setitem(DELEGATION_PRESSURE, "enabled", False)

    chain = agent_core._build_middlewares(
        fallback_chain=None,
        auxiliary_llm=None,
        main_llm_context_window=131_072,
        compression_trigger_ratio=0.8,
    )

    names = [type(mw).__name__ for mw in chain]
    assert names == EXPECTED_ORDER[:-1]
    assert "DelegationPressureGate" not in names


class _Checkpointer:
    async def setup(self) -> None:
        return None

    async def aclean_old_checkpoints(self) -> None:
        return None


class _MainLLM:
    def bind(self, **_: Any) -> "_MainLLM":
        return self


@pytest.mark.asyncio
async def test_built_agent_middleware_order_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _record_create_agent(**kwargs: Any) -> object:
        captured["middleware"] = kwargs["middleware"]
        return object()

    async def _checkpointer() -> _Checkpointer:
        return _Checkpointer()

    monkeypatch.setattr(agent_core, "_agent", None)
    monkeypatch.setattr(agent_core, "_agent_loop", None)
    monkeypatch.setattr(agent_core, "build_async_sqlite_checkpointer", _checkpointer)
    monkeypatch.setattr(agent_core, "build_main_llm", lambda: _MainLLM())
    monkeypatch.setattr(agent_core, "build_auxiliary_llm", lambda: object())
    _compression = object()
    monkeypatch.setattr(agent_core, "build_compression_llm", lambda: _compression)
    monkeypatch.setattr(agent_core, "build_fallback_chain", lambda: object())
    monkeypatch.setattr(agent_core, "get_agent_tools", lambda: [])
    monkeypatch.setattr(agent_core, "create_agent", _record_create_agent)
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "200000")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "200000")
    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "0")
    monkeypatch.setitem(DELEGATION_PRESSURE, "enabled", True)
    monkeypatch.setattr(agent_core, "main_llm_max_tokens", 65_536)
    from agent.wrapper import registry as wrapper_registry

    monkeypatch.setattr(wrapper_registry, "main_llm_max_tokens", 65_536)

    # No middleware stand-ins: the chain is now derived from
    # agent.middlewares.specs, and the real constructors are cheap enough to
    # build under the faked LLM/checkpointer seam. Their runtime class/name is
    # exactly what the pinned contract checks.
    await agent_core.built_agent()

    names = [getattr(item, "name", type(item).__name__) for item in captured["middleware"]]
    assert names == EXPECTED_ORDER
    # The single spec table is the source of truth, so the catalog projection
    # must agree with the assembled chain (no second hand-maintained list).
    from agent.middlewares.catalog import MIDDLEWARE_ORDER

    assert names == list(MIDDLEWARE_ORDER)
    assert len(set(names)) == len(names)
