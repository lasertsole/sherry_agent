"""Registration lock for the ``@dynamic_prompt`` system-prompt middleware.

``built_agent()`` must register ``system_prompt_injection`` exactly once, at
position 1 — right after ``TodoContinuationEnforcer`` (which implements no
model-call wrap) — making it the outermost ``wrap_model_call`` layer.
"""

from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware

from agent import core as agent_core
from agent.middlewares.system_prompt.core import system_prompt_injection
from agent.middlewares.todo_continuation import TodoContinuationEnforcer

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

_OTHER_MIDDLEWARE = (
    "Summarization",
    "ToolCallNormalize",
    "MultimodalProcessor",
    "ToolGuardrails",
    "IterationBudget",
    "HeartbeatStaleness",
    "OutputRepetitionGuard",
    "MaxTokensBoostMiddleware",
    "LLMRetryMiddleware",
    "HumanInTheLoop",
    "HITLConfig",
    "SubagentCompletionDrainMiddleware",
    "TaskIntentMiddleware",
    "TodoContinuationEnforcer",
)


class _Checkpointer:
    async def setup(self) -> None:
        return None

    async def aclean_old_checkpoints(self) -> None:
        return None


class _MainLLM:
    def bind(self, **_: Any) -> "_MainLLM":
        return self


@pytest.mark.asyncio
async def test_system_prompt_injection_is_outermost_wrap_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    for middleware_name in _OTHER_MIDDLEWARE:
        monkeypatch.setattr(agent_core, middleware_name, lambda *_, **__: object())

    await agent_core.built_agent()

    middleware = captured["middleware"]
    assert sum(item is system_prompt_injection for item in middleware) == 1
    assert middleware[1] is system_prompt_injection
    assert isinstance(system_prompt_injection, AgentMiddleware)
    assert system_prompt_injection.name == "system_prompt_injection"
    assert TodoContinuationEnforcer.wrap_model_call is AgentMiddleware.wrap_model_call
    assert TodoContinuationEnforcer.awrap_model_call is AgentMiddleware.awrap_model_call
