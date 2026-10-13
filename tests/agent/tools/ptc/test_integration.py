"""Module tests for PTC injection and the functional-role grid.

Hermetic: the child agent construction is intercepted at ``create_agent`` and
the checkpointer/model seams, mirroring the established librarian-role tests.
The role grid covers all five functional roles plus the main tool registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import _build_child_agent
from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole
from agent.tools.subagent.types.functional_role import PTC_ROLES
from agent.tools.ptc import build_ptc_tool

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

_MAIN_AGENT_ROLES = (
    FunctionalRole.GENERAL,
    FunctionalRole.RESEARCHER,
    FunctionalRole.REVIEWER,
    FunctionalRole.LIBRARIAN,
)

_DEFAULT_ALLOW = ["read_file", "write_file", "patch_file", "terminal", "python_repl"]


class _StubTool:
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture()
def _wiring(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured["tools"] = kwargs.get("tools", [])
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: SimpleNamespace(kind="main"))
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: SimpleNamespace(kind="aux"))
    return captured


def _build_child(captured: dict, functional_role: FunctionalRole, **kwargs) -> set[str]:
    asyncio.run(
        _build_child_agent(
            system_prompt="child",
            tools=[_StubTool(name) for name in _DEFAULT_ALLOW],
            functional_role=functional_role,
            **kwargs,
        )
    )
    return {tool.name for tool in captured["tools"]}


def test_ptc_roles_constant_is_executor_only() -> None:
    assert PTC_ROLES == frozenset({FunctionalRole.EXECUTOR})
    assert FunctionalRole.EXECUTOR in PTC_ROLES
    for role in _MAIN_AGENT_ROLES:
        assert role not in PTC_ROLES


def test_main_tool_registry_never_contains_ptc() -> None:
    source = Path(spawn_core.__file__).resolve().parents[3] / "tools" / "__init__.py"
    text = source.read_text(encoding="utf-8")
    assert "build_ptc_tool" not in text
    assert "execute_code" not in text

    from agent.tools import build_main_tools

    assert "execute_code" not in {tool.name for tool in build_main_tools()}


def test_executor_child_gets_execute_code(_wiring: dict) -> None:
    names = _build_child(
        _wiring,
        FunctionalRole.EXECUTOR,
        tool_allow=_DEFAULT_ALLOW,
        tool_deny=[],
        model_tier="auxiliary",
    )
    assert "execute_code" in names


@pytest.mark.parametrize("role", _MAIN_AGENT_ROLES, ids=lambda r: r.value)
def test_non_executor_child_has_no_execute_code(_wiring: dict, role: FunctionalRole) -> None:
    names = _build_child(
        _wiring,
        role,
        tool_allow=["read_file", "terminal", "web_search"],
        tool_deny=[],
        model_tier="auxiliary",
    )
    assert "execute_code" not in names


def test_ptc_whitelist_excludes_self_and_privileged_tools() -> None:
    tool = build_ptc_tool(
        available_tools=[
            _StubTool("read_file"),
            _StubTool("execute_code"),
            _StubTool("sessions_spawn"),
            _StubTool("sessions_kill"),
            _StubTool("memory"),
            _StubTool("skill_manage"),
            _StubTool("question"),
        ],
        session_id="agent:executor:subagent:grid",
    )
    assert "execute_code" not in tool._tools_map
    assert not {
        "execute_code",
        "sessions_spawn",
        "sessions_kill",
        "memory",
        "skill_manage",
        "question",
    } & set(tool._tools_map)


@pytest.mark.parametrize(
    "role", [FunctionalRole.EXECUTOR, *_MAIN_AGENT_ROLES], ids=lambda r: r.value
)
def test_prompt_guidance_present_only_for_executor(role: FunctionalRole) -> None:
    prompt = build_subagent_system_prompt(
        SubagentSessionRole.LEAF,
        "do the task",
        functional_role=role,
    )
    present = "## Programmatic Tool Calling (execute_code)" in prompt
    assert present == (role is FunctionalRole.EXECUTOR)


@pytest.fixture()
def _leaf_depth(monkeypatch):
    monkeypatch.setattr(get_config(), "max_spawn_depth", 1)
    yield


@pytest.fixture()
def _captured_lane(monkeypatch):
    captured: list[dict] = []

    async def _fake_lane(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    yield captured
    clear_registry()


def test_executor_spawn_end_to_end_injects_ptc_guidance(_leaf_depth, _captured_lane) -> None:
    async def _spawn():
        result = await spawn_core.spawn_subagent_direct(
            task="Implement the change",
            requester_session_key="agent:main:session:test",
            functional_role_hint="executor",
        )
        await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_spawn())
    assert result.status == "accepted", result.error
    lane = _captured_lane[-1]
    assert lane["run"].functional_role is FunctionalRole.EXECUTOR
    assert lane["model_tier"] == "auxiliary"
    assert "## Programmatic Tool Calling (execute_code)" in lane["system_prompt"]


def test_researcher_spawn_has_no_ptc_guidance(_leaf_depth, _captured_lane) -> None:
    async def _spawn():
        result = await spawn_core.spawn_subagent_direct(
            task="Investigate the code",
            requester_session_key="agent:main:session:test",
            functional_role_hint="researcher",
        )
        await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_spawn())
    assert result.status == "accepted", result.error
    lane = _captured_lane[-1]
    assert lane["run"].functional_role is FunctionalRole.RESEARCHER
    assert "## Programmatic Tool Calling (execute_code)" not in lane["system_prompt"]
