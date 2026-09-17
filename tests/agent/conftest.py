"""Shared fixtures for tests/agent/*.

``patched_agent_core`` reduces ``agent.core.built_agent`` to its build/wrap
seam: every heavy dependency (SQLite checkpointer, LLM clients, middleware
pipeline, graph compilation) is replaced with an in-process fake, so the
tests observe only the control flow that assembles and wraps the graph.
"""

from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture
def patched_agent_core(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Any]]:
    """Yield ``(agent_core, fake_compiled_graph)`` with built_agent's deps faked.

    ``agent_core`` is the REAL ``agent.core`` module; the fixture asserts that
    so a subagent-suite stub module can never produce a misleading pass.
    The fake compiled graph is the object ``create_agent`` returns — the raw
    innermost graph that the wrapper chain must wrap.
    """
    from agent import core as agent_core

    assert getattr(agent_core, "__file__", None) is not None, (
        "expected the real agent.core module, got the subagent-suite stub"
    )

    class _FakeCheckpointer:
        async def setup(self) -> None:
            return None

        async def aclean_old_checkpoints(self) -> None:
            return None

    class _FakeMainLLM:
        def bind(self, **_: Any) -> "_FakeMainLLM":
            return self

    fake_compiled_graph = object()

    async def _fake_checkpointer() -> _FakeCheckpointer:
        return _FakeCheckpointer()

    monkeypatch.setattr(agent_core, "_agent", None)
    monkeypatch.setattr(agent_core, "_agent_loop", None)
    monkeypatch.setattr(agent_core, "build_async_sqlite_checkpointer", _fake_checkpointer)
    monkeypatch.setattr(agent_core, "build_main_llm", lambda: _FakeMainLLM())
    monkeypatch.setattr(agent_core, "build_auxiliary_llm", lambda: object())
    monkeypatch.setattr(agent_core, "build_fallback_chain", lambda: object())
    monkeypatch.setattr(agent_core, "get_agent_tools", lambda: [])
    monkeypatch.setattr(agent_core, "create_agent", lambda **_: fake_compiled_graph)
    # CI runs without .env: give the MAX_TOKEN gate a valid pair through the
    # real env vars (never by patching the guard itself).
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    # The gate passes, but models.LLMs.main_llm.max_tokens is still None in CI,
    # so built_agent's trigger math (main_llm_max_tokens *
    # COMPRESSION_TRIGGER_RATIO) would raise. Pin the window deterministically
    # on both consumers that snapshot it at import.
    context_window = 65_536
    monkeypatch.setattr(agent_core, "main_llm_max_tokens", context_window)
    from agent.wrapper import registry as wrapper_registry

    monkeypatch.setattr(wrapper_registry, "main_llm_max_tokens", context_window)
    for middleware_name in (
        "Summarization",
        "ToolCallNormalize",
        "MultimodalProcessor",
        "context_engine_prompt",
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
    ):
        monkeypatch.setattr(agent_core, middleware_name, lambda *_, **__: object())

    yield agent_core, fake_compiled_graph
