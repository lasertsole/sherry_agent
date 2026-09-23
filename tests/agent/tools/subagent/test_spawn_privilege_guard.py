"""Spawn-privilege boundary tests (A1 pipeline intersection, A2 call-time guard).

A1: a functional-role whitelist (or a per-spawn extra tool) must never grant
``sessions_spawn`` / ``sessions_yield`` to a role that cannot spawn children;
ORCHESTRATOR behavior is unchanged. A2: the tool refuses the call at runtime for
a LEAF caller even when a tool instance leaked into its toolset.
"""

import asyncio

import pytest

import agent.tools.subagent.spawn.privilege as privilege
from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.roles.loader import RoleDefinition
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import SpawnResult, spawn_subagent_direct
from agent.tools.subagent.types import FunctionalRole
from agent.tools.subagent.types.capability import SubagentSessionRole

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def _captured_lane(monkeypatch):
    captured: list[dict] = []

    async def _fake_lane(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    yield captured
    clear_registry()


def _spawn(captured: list[dict], **kwargs) -> SpawnResult:
    async def _go():
        result = await spawn_subagent_direct(
            task="do the task",
            requester_session_key="agent:main:session:test",
            **kwargs,
        )
        await asyncio.sleep(0.01)
        return result

    return asyncio.run(_go())


def _spawn_yield_role_def(role: FunctionalRole, tools: list[str]) -> RoleDefinition:
    return RoleDefinition(
        role=role,
        description="test role",
        model_tier="auxiliary",
        tools=tools,
        prompt_body="body",
    )


class TestA1WhitelistIntersection:
    def test_leaf_role_whitelist_strips_spawn_and_yield(self, monkeypatch, _captured_lane):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 1)  # child depth 1 → LEAF
        monkeypatch.setattr(
            spawn_core,
            "load_role_definition",
            lambda role: _spawn_yield_role_def(
                role, ["read_file", "sessions_spawn", "sessions_yield"]
            ),
        )

        _spawn(_captured_lane, functional_role_hint="researcher")

        run = _captured_lane[-1]["run"]
        assert run.role is SubagentSessionRole.LEAF
        assert run.inherited_tool_allow == ["read_file"]

    def test_orchestrator_role_whitelist_keeps_spawn_and_yield(self, monkeypatch, _captured_lane):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)  # child depth 1 → ORCHESTRATOR
        monkeypatch.setattr(
            spawn_core,
            "load_role_definition",
            lambda role: _spawn_yield_role_def(role, ["read_file"]),
        )

        _spawn(_captured_lane, functional_role_hint="researcher")

        run = _captured_lane[-1]["run"]
        assert run.role is SubagentSessionRole.ORCHESTRATOR
        assert "read_file" in run.inherited_tool_allow
        assert "sessions_spawn" in run.inherited_tool_allow
        assert "sessions_yield" in run.inherited_tool_allow

    def test_leaf_extra_tools_cannot_grant_spawn(self, monkeypatch, _captured_lane):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 1)
        monkeypatch.setattr(
            spawn_core,
            "load_role_definition",
            lambda role: _spawn_yield_role_def(role, ["read_file"]),
        )

        _spawn(
            _captured_lane,
            functional_role_hint="researcher",
            extra_tools=["sessions_spawn"],
        )

        run = _captured_lane[-1]["run"]
        assert run.inherited_tool_allow == ["read_file"]

    @pytest.mark.parametrize(
        ("hint", "expected_allow"),
        [
            ("researcher", ["read_file", "terminal", "web_search"]),
            ("executor", ["read_file", "write_file", "patch_file", "terminal", "python_repl"]),
            ("reviewer", ["read_file", "terminal"]),
            ("librarian", ["read_file", "terminal", "web_search"]),
        ],
    )
    def test_builtin_roles_leaf_whitelist_unchanged(
        self, monkeypatch, _captured_lane, hint, expected_allow
    ):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 1)

        _spawn(_captured_lane, functional_role_hint=hint)

        run = _captured_lane[-1]["run"]
        assert run.inherited_tool_allow == expected_allow
        assert "sessions_spawn" not in run.inherited_tool_allow

    def test_general_role_leaf_inherit_mode_unchanged(self, monkeypatch, _captured_lane):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 1)

        _spawn(_captured_lane)

        run = _captured_lane[-1]["run"]
        assert run.functional_role is FunctionalRole.GENERAL
        assert run.inherited_tool_allow == []
        assert run.inherited_tool_deny == ["sessions_spawn", "sessions_yield"]

    @pytest.mark.parametrize("hint", ["researcher", "executor", "reviewer", "librarian"])
    def test_builtin_roles_orchestrator_keep_spawn(self, monkeypatch, _captured_lane, hint):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)

        _spawn(_captured_lane, functional_role_hint=hint)

        run = _captured_lane[-1]["run"]
        assert "sessions_spawn" in run.inherited_tool_allow
        assert "sessions_yield" in run.inherited_tool_allow


class TestA2CallTimeGuard:
    def test_resolve_caller_session_key(self):
        assert privilege.resolve_caller_session_key("abc") == "agent:main:session:abc"
        assert (
            privilege.resolve_caller_session_key("agent:main:subagent:x") == "agent:main:subagent:x"
        )
        assert (
            privilege.resolve_caller_session_key("agent:main:swarm:g:x") == "agent:main:swarm:g:x"
        )
        assert privilege.resolve_caller_session_key("  ") == ""

    @pytest.mark.parametrize(("depth", "allowed"), [(0, True), (1, True), (2, False)])
    def test_depth_role_verdict(self, monkeypatch, depth, allowed):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)
        monkeypatch.setattr(privilege, "get_subagent_depth", lambda _key: depth)

        verdict, _reason = privilege.check_spawn_permission("agent:main:subagent:x")

        assert verdict is allowed

    def test_leaf_reason_names_the_role(self, monkeypatch):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)
        monkeypatch.setattr(privilege, "get_subagent_depth", lambda _key: 2)

        allowed, reason = privilege.check_spawn_permission("agent:main:subagent:x")

        assert allowed is False
        assert "leaf" in reason

    @pytest.mark.asyncio
    async def test_build_time_tool_denies_leaf(self, monkeypatch):
        from agent.tools.subagent.tools import sessions_spawn as mod

        calls: list = []

        async def _fake_spawn(**kwargs):
            calls.append(kwargs)
            return SpawnResult(status="accepted", run_id="r", child_session_key="c")

        monkeypatch.setattr(mod, "spawn_subagent_direct", _fake_spawn)
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)
        monkeypatch.setattr(privilege, "get_subagent_depth", lambda _key: 2)
        tool = mod.SessionsSpawnTool(session_id="agent:main:subagent:leaf")

        out = await tool._arun(task="do the task")

        assert "denied" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_build_time_tool_allows_orchestrator(self, monkeypatch):
        from agent.tools.subagent.tools import sessions_spawn as mod

        calls: list = []

        async def _fake_spawn(**kwargs):
            calls.append(kwargs)
            return SpawnResult(status="accepted", run_id="r", child_session_key="c")

        monkeypatch.setattr(mod, "spawn_subagent_direct", _fake_spawn)
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)
        monkeypatch.setattr(privilege, "get_subagent_depth", lambda _key: 1)
        tool = mod.SessionsSpawnTool(session_id="agent:main:subagent:orch")

        out = await tool._arun(task="do the task")

        assert "denied" not in out
        assert calls and calls[0]["task"] == "do the task"

    @pytest.mark.asyncio
    async def test_runtime_tool_denies_leaf(self, monkeypatch):
        import agent.tools.subagent.spawn as spawn_pkg
        from agent.tools.subagent.tools.runtime_tools import build_subagent_runtime_tools

        calls: list = []

        async def _fake_spawn(**kwargs):
            calls.append(kwargs)
            return SpawnResult(status="accepted", run_id="r", child_session_key="c")

        monkeypatch.setattr(spawn_pkg, "spawn_subagent_direct", _fake_spawn)
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)
        monkeypatch.setattr(privilege, "get_subagent_depth", lambda _key: 2)
        tools = {t.name: t for t in build_subagent_runtime_tools()}

        out = await tools["sessions_spawn"].coroutine(
            task="do the task", session_id="agent:main:subagent:leaf"
        )

        assert "denied" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_runtime_tool_allows_orchestrator(self, monkeypatch):
        import agent.tools.subagent.spawn as spawn_pkg
        from agent.tools.subagent.tools.runtime_tools import build_subagent_runtime_tools

        calls: list = []

        async def _fake_spawn(**kwargs):
            calls.append(kwargs)
            return SpawnResult(status="accepted", run_id="r", child_session_key="c")

        monkeypatch.setattr(spawn_pkg, "spawn_subagent_direct", _fake_spawn)
        monkeypatch.setattr(get_config(), "max_spawn_depth", 2)
        monkeypatch.setattr(privilege, "get_subagent_depth", lambda _key: 1)
        tools = {t.name: t for t in build_subagent_runtime_tools()}

        out = await tools["sessions_spawn"].coroutine(
            task="do the task", session_id="agent:main:subagent:orch"
        )

        assert "denied" not in out
        assert calls and calls[0]["task"] == "do the task"
