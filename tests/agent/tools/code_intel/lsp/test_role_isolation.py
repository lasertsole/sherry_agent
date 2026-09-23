"""Role isolation: the LSP tools reach RESEARCHER children and nowhere else."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

LSP_NAMES = {
    "lsp_goto_definition",
    "lsp_find_references",
    "lsp_workspace_symbol",
    "lsp_call_hierarchy",
}
CODE_INTEL_NAMES = {"explore", "callers", "callees", "impact"}
AST_GREP_NAMES = {"ast_grep_search", "ast_grep_rewrite"}


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


def _build(**kwargs) -> None:
    from agent.tools.subagent.spawn.core import _build_child_agent

    asyncio.run(_build_child_agent(system_prompt="child", **kwargs))


def _tool_names(captured: dict) -> set[str]:
    return {tool.name for tool in captured["tools"]}


def _role(value: str):
    from agent.tools.subagent.types import FunctionalRole

    return FunctionalRole(value)


class TestMainAgentHasNoLsp:
    def test_main_builders_reference_no_lsp(self) -> None:
        from agent.tools import _MAIN_TOOLS_BUILDERS

        for builder in _MAIN_TOOLS_BUILDERS:
            module = getattr(builder, "__module__", "")
            qualname = getattr(builder, "__qualname__", "")
            assert "code_intel.lsp" not in module
            assert "build_lsp_tools" not in qualname

    def test_agent_tools_package_does_not_expose_lsp(self) -> None:
        import agent.tools as agent_tools

        assert not hasattr(agent_tools, "build_lsp_tools")
        source = Path(agent_tools.__file__).read_text(encoding="utf-8")
        assert "build_lsp_tools" not in source


class TestResearcherGetsLsp:
    def test_researcher_child_gets_lsp_code_intel_and_ast_grep(self, _wiring: dict) -> None:
        _build(
            tools=[_StubTool("read_file")],
            tool_allow=["read_file"],
            tool_deny=[],
            functional_role=_role("researcher"),
            model_tier="auxiliary",
        )
        names = _tool_names(_wiring)
        assert LSP_NAMES <= names
        assert CODE_INTEL_NAMES <= names
        assert AST_GREP_NAMES <= names


class TestOtherRolesHaveNoLsp:
    @pytest.mark.parametrize("role_name", ["general", "executor", "reviewer"])
    def test_non_researcher_child_has_no_lsp(self, _wiring: dict, role_name: str) -> None:
        _build(
            tools=[_StubTool("read_file"), _StubTool("write_file"), _StubTool("terminal")],
            tool_allow=["read_file", "write_file", "terminal", "python_repl"],
            tool_deny=[],
            functional_role=_role(role_name),
            model_tier="auxiliary",
        )
        assert not (LSP_NAMES & _tool_names(_wiring))
        assert AST_GREP_NAMES <= _tool_names(_wiring)
