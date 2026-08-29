"""Module tests for the frontmatter ``scope:`` skill visibility system.

Covers the shared contract end-to-end:

- ``skills.loader.scan_skills`` parses ``scope:`` (default/invalid -> "all")
  and carries it into the snapshot payload.
- ``skills.loader.get_skills_text`` filters by caller scope
  (main / subagent) — replacing the old hardcoded auth-skill exclusion.
- ``skill_view`` denies main_only skills to subagent callers.
- ``skill_list`` filters by the tool's ``metadata["caller_scope"]``.
- ``skill_manage`` validates the ``scope`` field in frontmatter and its
  builder tags the tool with ``metadata["scope"] = "main_only"``.
"""

import json

import pytest

import skills.loader as loader_module
from skills.loader import scan_skills, get_skills_text

import agent.tools.skill_tools.skill_view as skill_view_module
from agent.tools.skill_tools.skill_view import SkillView
from agent.tools.skill_tools.skill_list import SkillList
from agent.tools.skill_tools.skill_manage import _validate_frontmatter, build_skill_manage_tool


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _write_skill(skills_dir, rel_dir: str, frontmatter: str) -> None:
    skill_dir = skills_dir / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}---\n\nBody of {rel_dir}.",
        encoding="utf-8",
    )


@pytest.fixture
def scope_skills_dir(tmp_path, monkeypatch):
    """Isolated skills dir with one skill per scope variant."""
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "builtin/core/vault_main",
        "name: vault_main\ndescription: Main only\nscope: main_only\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/quiet_sub",
        "name: quiet_sub\ndescription: Subagent only\nscope: subagent_only\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/open_skill",
        "name: open_skill\ndescription: Everyone\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/bogus_scope",
        "name: bogus_scope\ndescription: Invalid scope value\nscope: banana\n",
    )
    monkeypatch.setattr(loader_module, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(loader_module, "ROOT_DIR", tmp_path)
    return skills_dir


_SCOPE_FIXTURE = [
    {
        "name": "shared_tool",
        "description": "d",
        "location": "./skills/builtin/core/shared_tool/SKILL.md",
        "scope": "all",
        "active": True,
    },
    {
        "name": "mainly",
        "description": "d",
        "location": "./skills/builtin/core/mainly/SKILL.md",
        "scope": "main_only",
        "active": True,
    },
    {
        "name": "subonly",
        "description": "d",
        "location": "./skills/builtin/core/subonly/SKILL.md",
        "scope": "subagent_only",
        "active": True,
    },
]


# ---------------------------------------------------------------------------
# scan_skills — scope parsing
# ---------------------------------------------------------------------------

class TestScanSkillsScope:
    def test_scope_parsed_and_carried(self, scope_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["vault_main"]["scope"] == "main_only"
        assert skills["quiet_sub"]["scope"] == "subagent_only"

    def test_absent_scope_defaults_to_all(self, scope_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["open_skill"]["scope"] == "all"

    def test_invalid_scope_falls_back_to_all(self, scope_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["bogus_scope"]["scope"] == "all"

    def test_scope_in_every_entry(self, scope_skills_dir):
        for s in scan_skills(use_cache=False):
            assert "scope" in s


# ---------------------------------------------------------------------------
# get_skills_text — caller-scope filtering
# ---------------------------------------------------------------------------

class TestGetSkillsTextScope:
    @pytest.fixture(autouse=True)
    def _patch_scan(self, monkeypatch):
        monkeypatch.setattr(loader_module, "scan_skills", lambda use_cache=True: list(_SCOPE_FIXTURE))

    def test_main_sees_all_except_subagent_only(self):
        xml = get_skills_text(caller_scope="main")
        assert "shared_tool" in xml
        assert "mainly" in xml
        assert "subonly" not in xml

    def test_subagent_sees_all_except_main_only(self):
        xml = get_skills_text(caller_scope="subagent")
        assert "shared_tool" in xml
        assert "subonly" in xml
        assert "mainly" not in xml

    def test_default_caller_scope_is_main(self):
        assert get_skills_text() == get_skills_text(caller_scope="main")

    def test_unknown_caller_scope_treated_as_main(self):
        assert get_skills_text(caller_scope="garbage") == get_skills_text(caller_scope="main")

    def test_selected_names_still_scope_filtered(self):
        # A subagent requesting a main_only skill by name gets nothing for it.
        xml = get_skills_text(selected_skill_names=["mainly"], caller_scope="subagent")
        assert "mainly" not in xml

        xml = get_skills_text(selected_skill_names=["mainly", "shared_tool"], caller_scope="subagent")
        assert "shared_tool" in xml
        assert "mainly" not in xml

    def test_xml_structure_preserved(self):
        xml = get_skills_text(caller_scope="subagent")
        assert xml.startswith("<available_skills>")
        assert xml.rstrip().endswith("</available_skills>")
        assert "<name>shared_tool</name>" in xml


# ---------------------------------------------------------------------------
# skill_view — denial for subagent callers
# ---------------------------------------------------------------------------

class TestSkillViewScope:
    @pytest.fixture
    def view_skills_dir(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _write_skill(
            skills_dir,
            "builtin/core/vault_main",
            "name: vault_main\ndescription: Main only\nscope: main_only\n",
        )
        _write_skill(
            skills_dir,
            "builtin/core/open_skill",
            "name: open_skill\ndescription: Everyone\n",
        )
        monkeypatch.setattr(skill_view_module, "SKILLS_DIR", skills_dir)
        return skills_dir

    @staticmethod
    def _run_view(caller_scope: str | None, name: str) -> dict:
        tool = SkillView()
        metadata = {"idempotent": True}
        if caller_scope is not None:
            metadata["caller_scope"] = caller_scope
        tool.metadata = metadata
        return json.loads(tool._run(name=name))

    def test_subagent_denied_main_only_skill(self, view_skills_dir):
        result = self._run_view("subagent", "vault_main")
        assert result["success"] is False
        assert "not visible" in result["error"]
        assert "main_only" in result["error"]

    def test_subagent_still_sees_default_scope_skill(self, view_skills_dir):
        result = self._run_view("subagent", "open_skill")
        assert result["success"] is True
        assert result["name"] == "open_skill"

    def test_main_unaffected_by_main_only_skill(self, view_skills_dir):
        result = self._run_view(None, "vault_main")  # no metadata -> main
        assert result["success"] is True

    def test_scope_read_from_frontmatter_not_snapshot(self, view_skills_dir):
        # The skill is not in any scan output cache — scope must come from the
        # SKILL.md frontmatter parse path.
        result = self._run_view("subagent", "vault_main")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# skill_list — caller-scope filtering
# ---------------------------------------------------------------------------

class TestSkillListScope:
    @pytest.fixture(autouse=True)
    def _patch_scan(self, monkeypatch):
        monkeypatch.setattr(
            loader_module, "scan_skills", lambda use_cache=True: list(_SCOPE_FIXTURE)
        )

    @staticmethod
    def _run_list(caller_scope: str | None) -> dict:
        tool = SkillList()
        metadata = {"idempotent": True}
        if caller_scope is not None:
            metadata["caller_scope"] = caller_scope
        tool.metadata = metadata
        return json.loads(tool._run(category=None))

    def test_main_sees_all_except_subagent_only(self):
        result = self._run_list(None)
        names = {s["name"] for s in result["skills"]}
        assert names == {"shared_tool", "mainly"}

    def test_subagent_sees_all_except_main_only(self):
        result = self._run_list("subagent")
        names = {s["name"] for s in result["skills"]}
        assert names == {"shared_tool", "subonly"}

    def test_scope_field_flows_through_output(self):
        result = self._run_list("subagent")
        scopes = {s["name"]: s.get("scope") for s in result["skills"]}
        assert scopes["shared_tool"] == "all"
        assert scopes["subonly"] == "subagent_only"


# ---------------------------------------------------------------------------
# skill_manage — frontmatter scope validation + tool metadata tag
# ---------------------------------------------------------------------------

class TestSkillManageScopeValidation:
    def _frontmatter(self, scope_line: str | None) -> str:
        lines = ["name: my_skill", "description: A test skill"]
        if scope_line is not None:
            lines.append(scope_line)
        return "---\n" + "\n".join(lines) + "\n---\n\nBody content."

    def test_valid_scopes_accepted(self):
        for scope in ("all", "main_only", "subagent_only"):
            assert _validate_frontmatter(self._frontmatter(f"scope: {scope}")) is None

    def test_absent_scope_accepted(self):
        assert _validate_frontmatter(self._frontmatter(None)) is None

    def test_invalid_scope_rejected(self):
        err = _validate_frontmatter(self._frontmatter("scope: banana"))
        assert err is not None
        assert "Invalid scope" in err
        assert "banana" in err

    def test_invalid_scope_rejected_in_patch_result(self):
        # _validate_frontmatter is the shared gate for create/edit/patch.
        err = _validate_frontmatter(self._frontmatter("scope: everyone"))
        assert "all, main_only, subagent_only" in err

    def test_builder_tags_tool_main_only(self):
        tool = build_skill_manage_tool()
        assert tool.metadata.get("scope") == "main_only"
        # Existing metadata is merged, not replaced.
        assert tool.metadata.get("idempotent") is False
