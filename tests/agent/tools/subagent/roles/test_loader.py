"""T1.2: functional-role definition loader — parsing, override, fail-open, cache."""

from pathlib import Path

import pytest

from agent.tools.subagent.roles import loader
from agent.tools.subagent.roles.loader import (
    get_roles_dir,
    invalidate_role_cache,
    load_all_role_definitions,
    load_role_definition,
)
from agent.tools.subagent.types import FunctionalRole

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _reset_role_cache():
    invalidate_role_cache()
    yield
    invalidate_role_cache()


def _write_override(tmp_path: Path, role: str, body: str) -> None:
    path = tmp_path / "subagent_roles" / role
    path.mkdir(parents=True)
    (path / "AGENTS.md").write_text(body, encoding="utf-8")


class TestBuiltinDefinitions:
    def test_roles_dir_is_tracked_package_path(self):
        assert get_roles_dir().is_dir()
        assert get_roles_dir().name == "definitions"

    def test_all_roles_load(self):
        defs = load_all_role_definitions()
        assert set(defs) == set(FunctionalRole)
        assert len(defs) == 5

    def test_researcher_frontmatter_parsed(self):
        definition = load_role_definition(FunctionalRole.RESEARCHER)
        assert definition is not None
        assert definition.description.startswith("Read-only research")
        assert definition.model_tier == "auxiliary"
        assert definition.tools == ["read_file", "terminal", "web_search"]

    def test_general_inherits_all_tools(self):
        definition = load_role_definition(FunctionalRole.GENERAL)
        assert definition is not None
        assert definition.tools is None

    def test_executor_tools_are_real_tool_names(self):
        definition = load_role_definition(FunctionalRole.EXECUTOR)
        assert definition is not None
        assert definition.tools == [
            "read_file",
            "write_file",
            "patch_file",
            "terminal",
            "python_repl",
        ]

    def test_reviewer_prompt_body_returned(self):
        definition = load_role_definition(FunctionalRole.REVIEWER)
        assert definition is not None
        assert "REVIEWER subagent worker" in definition.prompt_body

    def test_librarian_frontmatter_parsed(self):
        definition = load_role_definition(FunctionalRole.LIBRARIAN)
        assert definition is not None
        assert definition.description.startswith("External codebase retrieval")
        assert definition.model_tier == "auxiliary"
        assert "THE LIBRARIAN" in definition.prompt_body

    def test_librarian_tools_are_real_tool_names(self):
        definition = load_role_definition(FunctionalRole.LIBRARIAN)
        assert definition is not None
        assert definition.tools == ["read_file", "terminal", "web_search", "search_files"]
        assert "web_fetch" not in definition.tools
        assert not {"write_file", "patch_file", "python_repl"} & set(definition.tools or [])
        from agent.tools.file_tools import build_search_files_tool

        assert build_search_files_tool().name == "search_files"


class TestFailOpen:
    def test_missing_definition_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        monkeypatch.setattr(loader, "_DEFINITIONS_DIR", tmp_path / "absent")
        assert load_role_definition(FunctionalRole.GENERAL) is None

    def test_file_without_frontmatter_returns_none(self, monkeypatch, tmp_path):
        _write_override(tmp_path, "researcher", "no frontmatter at all")
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        assert load_role_definition(FunctionalRole.RESEARCHER) is None

    def test_librarian_malformed_frontmatter_returns_none(self, monkeypatch, tmp_path):
        _write_override(tmp_path, "librarian", "no frontmatter at all")
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        assert load_role_definition(FunctionalRole.LIBRARIAN) is None

    def test_malformed_tools_returns_none(self, monkeypatch, tmp_path):
        _write_override(
            tmp_path,
            "executor",
            "---\nname: executor\ndescription: x\nmodel_tier: auxiliary\ntools: 123\n---\nbody\n",
        )
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        assert load_role_definition(FunctionalRole.EXECUTOR) is None

    def test_invalid_model_tier_falls_back_to_inherit(self, monkeypatch, tmp_path):
        _write_override(
            tmp_path,
            "reviewer",
            "---\nname: reviewer\ndescription: x\nmodel_tier: gigantic\ntools:\n  - read_file\n---\nbody\n",
        )
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        definition = load_role_definition(FunctionalRole.REVIEWER)
        assert definition is not None
        assert definition.model_tier == "inherit"


class TestWorkspaceOverride:
    def test_override_wins_over_package_default(self, monkeypatch, tmp_path):
        _write_override(
            tmp_path,
            "researcher",
            "---\nname: researcher\ndescription: Custom override\n"
            "model_tier: main\ntools:\n  - read_file\n---\nCUSTOM BODY\n",
        )
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        definition = load_role_definition(FunctionalRole.RESEARCHER)
        assert definition is not None
        assert definition.description == "Custom override"
        assert definition.model_tier == "main"
        assert definition.tools == ["read_file"]
        assert definition.prompt_body == "CUSTOM BODY"

    def test_librarian_override_wins_over_package_default(self, monkeypatch, tmp_path):
        _write_override(
            tmp_path,
            "librarian",
            "---\nname: librarian\ndescription: Custom librarian\n"
            "model_tier: main\ntools:\n  - read_file\n---\nCUSTOM LIBRARIAN BODY\n",
        )
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        definition = load_role_definition(FunctionalRole.LIBRARIAN)
        assert definition is not None
        assert definition.description == "Custom librarian"
        assert definition.model_tier == "main"
        assert definition.tools == ["read_file"]
        assert definition.prompt_body == "CUSTOM LIBRARIAN BODY"


class TestCache:
    def test_load_all_is_cached(self):
        first = load_all_role_definitions()
        assert load_all_role_definitions() is first

    def test_invalidate_reloads_override(self, monkeypatch, tmp_path):
        first = load_all_role_definitions()
        _write_override(
            tmp_path,
            "executor",
            "---\nname: executor\ndescription: Overridden executor\n"
            "model_tier: auxiliary\ntools: inherit\n---\nBODY\n",
        )
        monkeypatch.setattr(loader, "WORKSPACE_DIR", tmp_path)
        assert load_all_role_definitions() is first

        invalidate_role_cache()
        refreshed = load_all_role_definitions()
        assert refreshed[FunctionalRole.EXECUTOR].description == "Overridden executor"
