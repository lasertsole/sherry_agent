"""Librarian functional role — resolution, prompt, and the full code-intel tool face.

Smoke: the ``librarian`` role resolves to ``FunctionalRole.LIBRARIAN`` (both the
``agent_id`` fallback and the ``functional_role`` hint) and the child receives
the complete code-intel surface (``explore`` / ``callers`` / ``callees`` /
``impact`` / ``semantic_code_search`` + the eight LSP tools) on top of its
read-only whitelist. Hermetic e2e drives the real ``spawn_subagent_direct``
resolution → definition → prompt pipeline through the lane seam.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import _build_child_agent, _resolve_functional_role
from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole
from agent.tools.subagent.roles import load_role_definition

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

CODE_INTEL_NAMES = {"explore", "callers", "callees", "impact", "semantic_code_search"}
LSP_NAMES = {
    "lsp_goto_definition",
    "lsp_find_references",
    "lsp_workspace_symbol",
    "lsp_call_hierarchy",
    "lsp_rename",
    "lsp_diagnostics",
    "lsp_format",
    "lsp_status",
}
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


def _tool_names(captured: dict) -> set[str]:
    return {tool.name for tool in captured["tools"]}


class TestLibrarianResolution:
    def test_agent_id_resolves_to_librarian(self) -> None:
        assert _resolve_functional_role(None, "librarian") is FunctionalRole.LIBRARIAN

    def test_hint_resolves_to_librarian(self) -> None:
        assert _resolve_functional_role("librarian", "main") is FunctionalRole.LIBRARIAN

    def test_definition_ships_in_package(self) -> None:
        definition = load_role_definition(FunctionalRole.LIBRARIAN)
        assert definition is not None
        assert definition.model_tier == "auxiliary"
        assert "THE LIBRARIAN" in definition.prompt_body


class TestLibrarianChildToolFace:
    def test_child_gets_whitelist_and_full_code_intel_surface(self, _wiring: dict) -> None:
        _build(
            tools=[
                _StubTool("read_file"),
                _StubTool("terminal"),
                _StubTool("web_search"),
                _StubTool("write_file"),
                _StubTool("sessions_spawn"),
            ],
            tool_allow=["read_file", "terminal", "web_search"],
            tool_deny=[],
            functional_role=FunctionalRole.LIBRARIAN,
            model_tier="auxiliary",
        )
        names = _tool_names(_wiring)
        assert {"read_file", "terminal", "web_search"} <= names
        assert CODE_INTEL_NAMES <= names
        assert LSP_NAMES <= names
        assert AST_GREP_NAMES <= names
        assert not {"write_file", "patch_file", "python_repl", "sessions_spawn"} & names


def _build(**kwargs) -> None:
    asyncio.run(_build_child_agent(system_prompt="child", **kwargs))


class TestLibrarianSystemPrompt:
    def test_code_intel_guidance_and_role_body_injected(self) -> None:
        definition = load_role_definition(FunctionalRole.LIBRARIAN)
        assert definition is not None
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Research the library",
            functional_role=FunctionalRole.LIBRARIAN,
            role_description=definition.description,
            role_prompt_body=definition.prompt_body,
        )
        assert "LIBRARIAN specialization" in prompt
        assert "THE LIBRARIAN" in prompt
        assert "## Code Intelligence Tools" in prompt
        assert "semantic_code_search" in prompt


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


class TestLibrarianSpawnE2E:
    def test_librarian_spawn_end_to_end(self, _leaf_depth, _captured_lane) -> None:
        async def _spawn():
            result = await spawn_core.spawn_subagent_direct(
                task="How does library X implement Y?",
                requester_session_key="agent:main:session:test",
                functional_role_hint="librarian",
            )
            await asyncio.sleep(0.01)
            return result

        result = asyncio.run(_spawn())
        assert result.status == "accepted", result.error
        lane = _captured_lane[-1]
        assert lane["run"].functional_role is FunctionalRole.LIBRARIAN
        assert lane["model_tier"] == "auxiliary"
        assert lane["run"].inherited_tool_allow == [
            "read_file",
            "terminal",
            "web_search",
        ]
        assert "THE LIBRARIAN" in lane["system_prompt"]
        assert "semantic_code_search" in lane["system_prompt"]
