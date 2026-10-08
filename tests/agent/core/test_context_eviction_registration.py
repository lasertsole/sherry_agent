"""Registration lock for ``ContextEvictionMiddleware`` (P0-2 / P1-9).

``built_agent()`` must register exactly one instance, listed after
``ToolGuardrails`` and before ``ToolCallNormalize``. In the wrap chain (first
registered = outermost) that position makes the eviction layer OUTER relative
to ``MessagePersistenceMiddleware``, which stays innermost: the tool result is
persisted in full first, and only then replaced by the preview on its way to
graph state. Its ``before_model`` hook therefore runs in list order before
``ToolCallNormalize`` / ``SubagentCompletionDrainMiddleware``, and after every
``before_agent`` hook (``MultimodalProcessor``) by graph topology.
"""

from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware

from agent import core as agent_core
from agent.middlewares.context_eviction import ContextEvictionMiddleware

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


class _NamedMiddleware:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.asyncio
async def test_context_eviction_registration_position(
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
        monkeypatch.setattr(
            agent_core,
            middleware_name,
            lambda *_, _name=middleware_name, **__: _NamedMiddleware(_name),
        )

    await agent_core.built_agent()

    middleware = captured["middleware"]
    names = [item.name for item in middleware]

    assert sum(isinstance(item, ContextEvictionMiddleware) for item in middleware) == 1
    assert names.count("ContextEvictionMiddleware") == 1

    eviction_index = names.index("ContextEvictionMiddleware")
    assert names.index("ToolGuardrails") < eviction_index
    assert eviction_index < names.index("ToolCallNormalize")
    # Outer relative to the persistence layer: persistence stays innermost.
    assert eviction_index < names.index("MessagePersistenceMiddleware")

    assert ContextEvictionMiddleware.wrap_tool_call is not AgentMiddleware.wrap_tool_call
    assert ContextEvictionMiddleware.awrap_tool_call is not AgentMiddleware.awrap_tool_call
    # P1-9 hooks share the same middleware: tagging + model-view truncation.
    assert ContextEvictionMiddleware.before_model is not AgentMiddleware.before_model
    assert ContextEvictionMiddleware.abefore_model is not AgentMiddleware.abefore_model
    assert ContextEvictionMiddleware.wrap_model_call is not AgentMiddleware.wrap_model_call
    assert ContextEvictionMiddleware.awrap_model_call is not AgentMiddleware.awrap_model_call
