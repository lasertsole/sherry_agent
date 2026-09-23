"""T1.3: functional-role integration — spawn record, LLM selection, tool whitelist, extra_tools."""

import asyncio
from types import SimpleNamespace

import pytest

from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import _build_child_agent, spawn_subagent_direct
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _StubTool:
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture
def _leaf_depth(monkeypatch):
    """Force child_depth 1 to be a LEAF so depth-role spawn/yield unlocks stay out of the way."""
    monkeypatch.setattr(get_config(), "max_spawn_depth", 1)
    yield


@pytest.fixture
def _captured_lane(monkeypatch):
    captured: list[dict] = []

    async def _fake_lane(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    yield captured
    clear_registry()


async def _spawn(captured: list[dict], **kwargs):
    result = await spawn_subagent_direct(
        task="research the codebase",
        requester_session_key="agent:main:session:test",
        **kwargs,
    )
    await asyncio.sleep(0.01)
    return result


class TestSpawnFunctionalRole:
    def test_researcher_record_llm_tier_and_whitelist(self, _leaf_depth, _captured_lane):
        result = asyncio.run(_spawn(_captured_lane, functional_role_hint="researcher"))
        assert result.status == "accepted"
        run = _captured_lane[-1]["run"]
        assert run.functional_role is FunctionalRole.RESEARCHER
        assert _captured_lane[-1]["model_tier"] == "auxiliary"
        assert run.inherited_tool_allow == ["read_file", "terminal", "web_search"]

    def test_executor_whitelist_has_write_but_no_spawn(self, _leaf_depth, _captured_lane):
        asyncio.run(_spawn(_captured_lane, functional_role_hint="executor"))
        run = _captured_lane[-1]["run"]
        assert run.functional_role is FunctionalRole.EXECUTOR
        assert "write_file" in run.inherited_tool_allow
        assert "sessions_spawn" not in run.inherited_tool_allow

    def test_reviewer_whitelist_is_read_only(self, _leaf_depth, _captured_lane):
        asyncio.run(_spawn(_captured_lane, functional_role_hint="reviewer"))
        run = _captured_lane[-1]["run"]
        assert run.inherited_tool_allow == ["read_file", "terminal"]
        assert "write_file" not in run.inherited_tool_allow

    def test_extra_tools_extend_whitelist(self, _leaf_depth, _captured_lane):
        asyncio.run(
            _spawn(
                _captured_lane,
                functional_role_hint="researcher",
                extra_tools=["python_repl"],
            )
        )
        run = _captured_lane[-1]["run"]
        assert "python_repl" in run.inherited_tool_allow
        assert _captured_lane[-1]["extra_tools"] == ["python_repl"]

    def test_unknown_hint_falls_back_to_general_default_path(self, _leaf_depth, _captured_lane):
        asyncio.run(_spawn(_captured_lane, functional_role_hint="wizard"))
        run = _captured_lane[-1]["run"]
        assert run.functional_role is FunctionalRole.GENERAL
        assert _captured_lane[-1]["model_tier"] is None
        assert run.inherited_tool_allow == []
        assert _captured_lane[-1]["extra_tools"] == []


@pytest.fixture
def _wiring(monkeypatch):
    """Stub every heavy dependency of _build_child_agent; capture the chosen model + tools."""
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["tools"] = kwargs.get("tools", [])
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)

    main_llm = SimpleNamespace(kind="main")
    aux_llm = SimpleNamespace(kind="aux")
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: main_llm)
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: aux_llm)
    captured["main_llm"] = main_llm
    captured["aux_llm"] = aux_llm
    return captured


def _build(**kwargs):
    return asyncio.run(_build_child_agent(system_prompt="child", **kwargs))


class TestRoleDrivenLlmSelection:
    def test_leaf_general_uses_auxiliary(self, _wiring):
        _build(tools=[], tool_allow=[], tool_deny=[], role=SubagentSessionRole.LEAF)
        assert _wiring["model"] is _wiring["aux_llm"]

    def test_orchestrator_general_uses_main(self, _wiring):
        _build(tools=[], tool_allow=[], tool_deny=[], role=SubagentSessionRole.ORCHESTRATOR)
        assert _wiring["model"] is _wiring["main_llm"]

    def test_orchestrator_researcher_uses_auxiliary(self, _wiring):
        _build(
            tools=[],
            tool_allow=["read_file"],
            tool_deny=[],
            role=SubagentSessionRole.ORCHESTRATOR,
            functional_role=FunctionalRole.RESEARCHER,
            model_tier="auxiliary",
        )
        assert _wiring["model"] is _wiring["aux_llm"]


class TestRoleToolWhitelist:
    def test_researcher_whitelist_filters_candidates(self, _wiring):
        candidates = [
            _StubTool("read_file"),
            _StubTool("write_file"),
            _StubTool("terminal"),
            _StubTool("web_search"),
            _StubTool("sessions_spawn"),
            _StubTool("memory", {"scope": "main_only"}),
        ]
        _build(
            tools=candidates,
            tool_allow=["read_file", "terminal", "web_search"],
            tool_deny=[],
            role=SubagentSessionRole.LEAF,
            functional_role=FunctionalRole.RESEARCHER,
            model_tier="auxiliary",
        )
        assert [t.name for t in _wiring["tools"]] == [
            "read_file",
            "terminal",
            "web_search",
            "explore",
            "callers",
            "callees",
            "impact",
            "lsp_goto_definition",
            "lsp_find_references",
            "lsp_workspace_symbol",
            "lsp_call_hierarchy",
            "ast_grep_search",
            "ast_grep_rewrite",
        ]

    def test_executor_whitelist_excludes_spawn(self, _wiring):
        candidates = [
            _StubTool("read_file"),
            _StubTool("write_file"),
            _StubTool("terminal"),
            _StubTool("sessions_spawn"),
        ]
        _build(
            tools=candidates,
            tool_allow=["read_file", "write_file", "patch_file", "terminal", "python_repl"],
            tool_deny=[],
            role=SubagentSessionRole.LEAF,
            functional_role=FunctionalRole.EXECUTOR,
            model_tier="auxiliary",
        )
        names = [t.name for t in _wiring["tools"]]
        assert "write_file" in names
        assert "sessions_spawn" not in names

    def test_extra_tools_join_active_whitelist(self, _wiring):
        candidates = [_StubTool("read_file"), _StubTool("python_repl"), _StubTool("write_file")]
        _build(
            tools=candidates,
            tool_allow=["read_file"],
            tool_deny=[],
            role=SubagentSessionRole.LEAF,
            functional_role=FunctionalRole.RESEARCHER,
            model_tier="auxiliary",
            extra_tools=["python_repl"],
        )
        assert [t.name for t in _wiring["tools"]] == [
            "read_file",
            "python_repl",
            "explore",
            "callers",
            "callees",
            "impact",
            "lsp_goto_definition",
            "lsp_find_references",
            "lsp_workspace_symbol",
            "lsp_call_hierarchy",
            "ast_grep_search",
            "ast_grep_rewrite",
        ]
