"""Per-role subagent models (预设-子代理模型): requester config → child LLM.

Two seams: ``resolve_role_model_profile`` reads the requester session's
``AGENT_CONFIG["subagent_models"]`` (mem-only, fail-open), and
``_build_child_agent`` prefers that profile over the role's ``model_tier`` by
building through ``build_main_llm_for_profile`` (the only constructor that can
switch provider / key / base_url). A profile that fails to build must degrade to
the tier, never fail the spawn.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import _build_child_agent
from agent.tools.subagent.spawn.plan import resolve_role_model_profile
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

SESSION = "parent-session"


class _StubTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolated_config():
    state_register_mem.clear_session(SESSION)
    yield
    state_register_mem.clear_session(SESSION)


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A child-agent build harness capturing the LLM and tools it receives."""
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["tools"] = kwargs.get("tools", [])
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*_args, **_kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: SimpleNamespace(kind="main"))
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: SimpleNamespace(kind="aux"))
    return captured


def _set_models(mapping: dict) -> None:
    state_register_mem.set_state(SESSION, StateKey.AGENT_CONFIG, {"subagent_models": mapping})


# ----------------------------------------------------------------------
# resolver
# ----------------------------------------------------------------------


def test_an_unset_config_means_follow_the_tier():
    assert resolve_role_model_profile(SESSION, FunctionalRole.RESEARCHER) is None
    assert resolve_role_model_profile(None, FunctionalRole.RESEARCHER) is None


def test_a_configured_role_returns_its_profile():
    _set_models({"researcher": {"model": "glm-4.6", "provider": "zhipu", "api_key": "k"}})

    assert resolve_role_model_profile(SESSION, FunctionalRole.RESEARCHER) == {
        "model": "glm-4.6",
        "provider": "zhipu",
        "api_key": "k",
    }
    # A role without an entry follows its tier, even when a sibling is configured.
    assert resolve_role_model_profile(SESSION, FunctionalRole.EXECUTOR) is None


def test_a_profile_without_a_model_name_is_ignored():
    _set_models({"executor": {"provider": "zhipu"}})

    assert resolve_role_model_profile(SESSION, FunctionalRole.EXECUTOR) is None


def test_the_profile_is_found_through_a_prefixed_session_key():
    state_register_mem.set_state(
        "smoke-session",
        StateKey.AGENT_CONFIG,
        {"subagent_models": {"librarian": {"model": "m"}}},
    )
    try:
        assert resolve_role_model_profile(
            "agent:main:session:smoke-session", FunctionalRole.LIBRARIAN
        ) == {"model": "m"}
    finally:
        state_register_mem.clear_session("smoke-session")


def test_a_broken_register_read_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    import runtime

    class _Boom:
        def get_state(self, *_args, **_kwargs):
            raise RuntimeError("register down")

    monkeypatch.setattr(runtime, "state_register_mem", _Boom())

    assert resolve_role_model_profile(SESSION, FunctionalRole.RESEARCHER) is None


# ----------------------------------------------------------------------
# child build
# ----------------------------------------------------------------------


def test_a_role_profile_builds_the_child_llm_from_the_descriptor(
    wiring: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def _fake_for_profile(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(kind="profile")

    monkeypatch.setattr("models.build_main_llm_for_profile", _fake_for_profile)

    asyncio.run(
        _build_child_agent(
            system_prompt="prompt",
            tools=[_StubTool("read_file")],
            tool_allow=None,
            tool_deny=[],
            role=SubagentSessionRole.LEAF,
            functional_role=FunctionalRole.RESEARCHER,
            model_profile={"model": "glm-4.6", "provider": "zhipu", "api_key": "k"},
        )
    )

    assert calls == [{"provider": "zhipu", "model": "glm-4.6", "api_key": "k", "base_url": None}]
    assert getattr(wiring["model"], "kind", None) == "profile", "the child must use that LLM"


def test_without_a_profile_the_role_tier_still_decides(
    wiring: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unexpected(**_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("build_main_llm_for_profile must not run without a profile")

    monkeypatch.setattr("models.build_main_llm_for_profile", _unexpected)

    asyncio.run(
        _build_child_agent(
            system_prompt="prompt",
            tools=[_StubTool("read_file")],
            tool_allow=None,
            tool_deny=[],
            role=SubagentSessionRole.LEAF,
            functional_role=FunctionalRole.RESEARCHER,
            model_tier="auxiliary",
        )
    )

    assert getattr(wiring["model"], "kind", None) == "aux"


def test_a_profile_that_fails_to_build_falls_back_to_the_tier(
    wiring: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kwargs):
        raise RuntimeError("bad provider")

    monkeypatch.setattr("models.build_main_llm_for_profile", _boom)

    asyncio.run(
        _build_child_agent(
            system_prompt="prompt",
            tools=[_StubTool("read_file")],
            tool_allow=None,
            tool_deny=[],
            role=SubagentSessionRole.LEAF,
            functional_role=FunctionalRole.RESEARCHER,
            model_tier="auxiliary",
            model_profile={"model": "broken", "provider": "nope"},
        )
    )

    assert getattr(wiring["model"], "kind", None) == "aux", "the spawn must survive a bad profile"


@pytest.mark.asyncio
async def test_the_lane_passes_the_profile_down_to_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane boundary forwards the resolved per-role profile to the executor."""
    from agent.tools.subagent.registry.run_manager import register_run

    captured: dict = {}

    async def _fake_execute(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent", _fake_execute)
    run = register_run(
        child_session_key="agent:main:subagent:role-model",
        requester_session_key="agent:main:session:role-model",
        task="t",
        depth=1,
    )

    await spawn_core._execute_subagent_with_lane(
        run=run,
        system_prompt="p",
        user_message="u",
        tools=[],
        timeout_seconds=0,
        model_profile={"model": "glm-4.6", "provider": "zhipu"},
    )

    assert captured.get("model_profile") == {"model": "glm-4.6", "provider": "zhipu"}
