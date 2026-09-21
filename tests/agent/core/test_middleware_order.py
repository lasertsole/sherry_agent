"""Order contract for the middleware pipeline assembled by ``built_agent()``.

The middleware list order is load-bearing: LangChain executes ``after_model``
nodes in reverse registration order and ``wrap_model_call`` layers
outermost-first. This pins the exact sequence produced by
``agent.core._build_middlewares`` through the real ``built_agent`` path, so a
builder refactor cannot silently reorder hooks.
"""

from typing import Any

import pytest

from agent import core as agent_core

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

EXPECTED_ORDER = [
    "TodoContinuationEnforcer",
    "system_prompt_injection",
    "MultimodalProcessor",
    "IterationBudget",
    "ToolGuardrails",
    "ContextEvictionMiddleware",
    "ToolCallNormalize",
    "PathGuard",
    "SubagentCompletionDrainMiddleware",
    "TaskIntentMiddleware",
    "OutputRepetitionGuard",
    "MaxTokensBoostMiddleware",
    "HeartbeatStaleness",
    "HumanInTheLoop",
    "MessagePersistenceMiddleware",
    "LLMRetryMiddleware",
    "Summarization",
]


class _NamedMiddleware:
    def __init__(self, name: str) -> None:
        self.name = name


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
    monkeypatch.setattr(agent_core, "build_fallback_chain", lambda: object())
    monkeypatch.setattr(agent_core, "get_agent_tools", lambda: [])
    monkeypatch.setattr(agent_core, "create_agent", _record_create_agent)
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    monkeypatch.setattr(agent_core, "main_llm_max_tokens", 65_536)
    from agent.wrapper import registry as wrapper_registry

    monkeypatch.setattr(wrapper_registry, "main_llm_max_tokens", 65_536)

    for name in EXPECTED_ORDER:
        if name == "system_prompt_injection":
            monkeypatch.setattr(agent_core, name, _NamedMiddleware(name))
        else:
            monkeypatch.setattr(agent_core, name, lambda *_, _n=name, **__: _NamedMiddleware(_n))

    await agent_core.built_agent()

    names = [item.name for item in captured["middleware"]]
    assert names == EXPECTED_ORDER
    assert len(set(names)) == len(names)
