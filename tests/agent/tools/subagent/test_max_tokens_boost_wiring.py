"""MaxTokensBoostMiddleware wiring in the subagent middleware stack.

Guards the token-limit plan Phase 3 rollout to child agents: the middleware
must sit in ``_build_child_agent``'s chain right after ``OutputRepetitionGuard``
(same relative position as in the main agent), and the non-streaming path must
be the default for children (they run via ``ainvoke``, so ``is_stream_turn`` is
never set for a child session id).
"""

import asyncio
from types import SimpleNamespace

import pytest

from agent.middlewares import MaxTokensBoostMiddleware
from agent.tools.subagent.spawn.core import _build_child_agent
from agent.tools.subagent.types import SubagentSessionRole

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture
def _wiring(monkeypatch):
    """Stub every heavy dependency of _build_child_agent; capture create_agent."""
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured["middleware"] = kwargs.get("middleware", [])
        captured["model"] = kwargs.get("model")
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)

    class _FakeLLM:
        pass

    # CI runs without .env: MAIN_LLM_MAX_TOKEN is unset and spawn/core would
    # crash on `None * COMPRESSION_TRIGGER_RATIO` — pin the window deterministically
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: _FakeLLM())
    return captured


def test_child_agent_includes_max_tokens_boost_after_repetition_guard(_wiring):
    asyncio.run(
        _build_child_agent(
            system_prompt="child",
            tools=[],
            tool_allow=None,
            tool_deny=None,
            role=SubagentSessionRole.LEAF,
        )
    )

    middleware = _wiring["middleware"]
    names = [type(mw).__name__ for mw in middleware]
    assert "MaxTokensBoostMiddleware" in names
    assert names.index("MaxTokensBoostMiddleware") == names.index("OutputRepetitionGuard") + 1, (
        f"unexpected middleware order: {names}"
    )


def test_child_boost_middleware_defaults_to_non_stream_path(_wiring):
    """Children never carry the parent's is_stream_turn flag → non-stream path
    (no callback stripping) is the correct default for ainvoke execution."""
    asyncio.run(
        _build_child_agent(
            system_prompt="child",
            tools=[],
            tool_allow=None,
            tool_deny=None,
            role=SubagentSessionRole.LEAF,
        )
    )

    boost = next(mw for mw in _wiring["middleware"] if isinstance(mw, MaxTokensBoostMiddleware))
    req = SimpleNamespace(state={"session_id": "agent:main:subagent:unknown-child"})
    assert boost._is_stream_turn(req) is False
