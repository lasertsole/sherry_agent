"""Registration lock for ``MessagePersistenceMiddleware``.

``built_agent()`` must register exactly one instance, listed after
``HumanInTheLoop`` and before ``Summarization``. LangChain 1.3.9 chains
``after_model`` nodes in REVERSE registration order (``factory.py``:
``model`` -> ``after_model[-1]`` -> ... -> ``after_model[0]``), so being later
than HITL in the list makes the persistence hook the FIRST to run after
``model``: the AI message is flushed before HITL strips denied tool calls or
raises ``GraphInterrupt``.
"""

from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware

from agent import core as agent_core
from agent.middlewares.message_persistence import MessagePersistenceMiddleware

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
async def test_message_persistence_registration_position(
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

    assert sum(isinstance(item, MessagePersistenceMiddleware) for item in middleware) == 1
    assert names.count("MessagePersistenceMiddleware") == 1
    assert len(set(names)) == len(names)

    persistence_index = names.index("MessagePersistenceMiddleware")
    assert names.index("HumanInTheLoop") < persistence_index
    assert persistence_index < names.index("Summarization")

    assert MessagePersistenceMiddleware.after_model is not AgentMiddleware.after_model
    assert MessagePersistenceMiddleware.aafter_model is not AgentMiddleware.aafter_model
