"""Role isolation: code_intel tools reach RESEARCHER children and nowhere else."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

CODE_INTEL_NAMES = {"explore", "callers", "callees", "impact"}
SEMANTIC_NAMES = {"semantic_code_search"}


class _StubTool:
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture()
def _wiring(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub every heavy dependency of _build_child_agent; capture the tool list."""
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


class TestMainAgentHasNoCodeIntel:
    def test_main_builders_reference_no_code_intel(self) -> None:
        from agent.tools import _MAIN_TOOLS_BUILDERS

        for builder in _MAIN_TOOLS_BUILDERS:
            module = getattr(builder, "__module__", "")
            qualname = getattr(builder, "__qualname__", "")
            assert "code_intel" not in module
            assert "code_intel" not in qualname

    def test_agent_tools_package_does_not_import_code_intel(self) -> None:
        import agent.tools as agent_tools

        assert not hasattr(agent_tools, "build_code_intel_tools")
        source = Path(agent_tools.__file__).read_text(encoding="utf-8")
        assert "code_intel" not in source

    def test_agent_tools_package_never_mentions_semantic_search(self) -> None:
        import agent.tools as agent_tools

        source = Path(agent_tools.__file__).read_text(encoding="utf-8")
        assert "semantic_code_search" not in source


class TestResearcherGetsCodeIntel:
    def test_researcher_child_gets_all_code_intel_tools(self, _wiring: dict) -> None:
        _build(
            tools=[_StubTool("read_file")],
            tool_allow=["read_file"],
            tool_deny=[],
            functional_role=_researcher(),
            model_tier="auxiliary",
        )
        names = _tool_names(_wiring)
        assert CODE_INTEL_NAMES <= names
        assert SEMANTIC_NAMES <= names
        assert "read_file" in names


class TestOtherRolesHaveNoCodeIntel:
    def test_executor_child_has_no_code_intel(self, _wiring: dict) -> None:
        _build(
            tools=[_StubTool("read_file"), _StubTool("write_file")],
            tool_allow=["read_file", "write_file", "terminal", "python_repl"],
            tool_deny=[],
            functional_role=_role("executor"),
            model_tier="auxiliary",
        )
        names = _tool_names(_wiring)
        assert not (CODE_INTEL_NAMES & names)
        assert not (SEMANTIC_NAMES & names)

    def test_reviewer_child_has_no_code_intel(self, _wiring: dict) -> None:
        _build(
            tools=[_StubTool("read_file"), _StubTool("terminal")],
            tool_allow=["read_file", "terminal"],
            tool_deny=[],
            functional_role=_role("reviewer"),
            model_tier="auxiliary",
        )
        names = _tool_names(_wiring)
        assert not (CODE_INTEL_NAMES & names)
        assert not (SEMANTIC_NAMES & names)

    def test_general_child_has_no_code_intel(self, _wiring: dict) -> None:
        _build(
            tools=[_StubTool("read_file"), _StubTool("terminal")],
            tool_allow=[],
            tool_deny=[],
            functional_role=_role("general"),
        )
        names = _tool_names(_wiring)
        assert not (CODE_INTEL_NAMES & names)
        assert not (SEMANTIC_NAMES & names)


def _researcher():
    from agent.tools.subagent.types import FunctionalRole

    return FunctionalRole.RESEARCHER


def _role(value: str):
    from agent.tools.subagent.types import FunctionalRole

    return FunctionalRole(value)
