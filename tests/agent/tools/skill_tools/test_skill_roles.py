"""Per-role skill isolation across the tool surface.

The ``roles: [...]`` frontmatter field restricts a skill to specific functional
subagent roles. These tests pin every consumer: the index (loader), `skill_list`,
`skill_view` (which reads the target frontmatter directly), and the write-side
validation in `skill_manage`.
"""

import json

import pytest

import skills.loader as loader_module
from agent.tools.skill_tools.skill_list import SkillList
from agent.tools.skill_tools.skill_manage import _validate_frontmatter
from agent.tools.skill_tools.skill_view import SkillView
import agent.tools.skill_tools.skill_view as skill_view_module

pytestmark = [pytest.mark.module]

_ROLE_FIXTURE = [
    {
        "name": "exec_only",
        "description": "d",
        "location": "./skills/builtin/core/exec_only/SKILL.md",
        "scope": "all",
        "roles": ["executor"],
        "active": True,
    },
    {
        "name": "research_pair",
        "description": "d",
        "location": "./skills/builtin/core/research_pair/SKILL.md",
        "scope": "all",
        "roles": ["researcher", "librarian"],
        "active": True,
    },
    {
        "name": "untagged",
        "description": "d",
        "location": "./skills/builtin/core/untagged/SKILL.md",
        "scope": "all",
        "roles": None,
        "active": True,
    },
]


class TestSkillListRoles:
    @pytest.fixture(autouse=True)
    def _patch_scan(self, monkeypatch):
        monkeypatch.setattr(
            loader_module, "scan_skills", lambda use_cache=True: list(_ROLE_FIXTURE)
        )

    @staticmethod
    def _run_list(caller_scope: str | None, caller_role: str | None) -> dict:
        tool = SkillList()
        metadata = {"idempotent": True}
        if caller_scope is not None:
            metadata["caller_scope"] = caller_scope
        if caller_role is not None:
            metadata["caller_role"] = caller_role
        tool.metadata = metadata
        return json.loads(tool._run(category=None))

    def test_executor_sees_its_skill_and_untagged(self):
        names = {s["name"] for s in self._run_list("subagent", "executor")["skills"]}
        assert names == {"exec_only", "untagged"}

    def test_researcher_cannot_see_the_executor_skill(self):
        names = {s["name"] for s in self._run_list("subagent", "researcher")["skills"]}
        assert "research_pair" in names
        assert "exec_only" not in names

    def test_identity_role_sees_everything(self):
        names = {s["name"] for s in self._run_list("subagent", None)["skills"]}
        assert names == {"exec_only", "research_pair", "untagged"}

    def test_main_sees_everything(self):
        names = {s["name"] for s in self._run_list(None, None)["skills"]}
        assert "exec_only" in names and "research_pair" in names


class TestSkillViewRoles:
    @pytest.fixture
    def view_skills_dir(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        for rel, frontmatter in (
            (
                "builtin/core/exec_only",
                "name: exec_only\ndescription: d\nroles: [executor]\n",
            ),
            (
                "builtin/core/untagged",
                "name: untagged\ndescription: d\n",
            ),
        ):
            skill_dir = skills_dir / rel
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\n{frontmatter}---\n\nBody of {rel}.",
                encoding="utf-8",
            )
        monkeypatch.setattr(skill_view_module, "SKILLS_DIR", skills_dir)
        return skills_dir

    @staticmethod
    def _run_view(caller_role: str | None, name: str) -> dict:
        tool = SkillView()
        metadata = {"idempotent": True, "caller_scope": "subagent"}
        if caller_role is not None:
            metadata["caller_role"] = caller_role
        tool.metadata = metadata
        return json.loads(tool._run(name=name))

    def test_a_foreign_role_is_refused(self, view_skills_dir):
        result = self._run_view("researcher", "exec_only")
        assert result["success"] is False
        assert "not visible to this role" in result["error"]
        assert "executor" in result["error"]

    def test_the_listed_role_is_allowed(self, view_skills_dir):
        result = self._run_view("executor", "exec_only")
        assert result["success"] is True
        assert result["name"] == "exec_only"

    def test_an_untagged_skill_is_allowed_for_every_role(self, view_skills_dir):
        result = self._run_view("researcher", "untagged")
        assert result["success"] is True

    def test_identity_role_skips_the_check(self, view_skills_dir):
        result = self._run_view(None, "exec_only")
        assert result["success"] is True


class TestSkillManageRolesValidation:
    @staticmethod
    def _content(frontmatter: str) -> str:
        return f"---\nname: probe\ndescription: d\n{frontmatter}---\n\nBody."

    def test_valid_roles_accepted(self):
        assert _validate_frontmatter(self._content("roles: [executor, reviewer]\n")) is None
        assert _validate_frontmatter(self._content("roles: []\n")) is None
        assert _validate_frontmatter(self._content("")) is None

    def test_unknown_role_rejected(self):
        error = _validate_frontmatter(self._content("roles: [banana]\n"))
        assert error is not None
        assert "Invalid role" in error

    def test_non_list_roles_rejected(self):
        error = _validate_frontmatter(self._content("roles: executor\n"))
        assert error is not None
        assert "must be a list" in error
