"""Per-role skill visibility (the ``roles:`` frontmatter field).

`roles` is ORTHOGONAL to `scope`: scope decides main vs subagent, roles decide
which functional subagent roles may see the skill. Absent roles = every role
(the backward-compatible default), so tagging is opt-in.
"""

import pytest

import skills.loader as loader_module
from skills.loader import get_skills_text, scan_skills, skills_catalog

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _write_skill(skills_dir, rel_dir: str, frontmatter: str) -> None:
    skill_dir = skills_dir / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}---\n\nBody of {rel_dir}.",
        encoding="utf-8",
    )


@pytest.fixture
def role_skills_dir(tmp_path, monkeypatch):
    """An isolated skills tree with one skill per roles variant."""
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "builtin/core/exec_only",
        "name: exec_only\ndescription: Executor only\nroles: [executor]\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/research_pair",
        "name: research_pair\ndescription: Research pair\nroles: [researcher, librarian]\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/untagged",
        "name: untagged\ndescription: Everyone\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/scalar_role",
        "name: scalar_role\ndescription: Scalar form\nroles: executor\n",
    )
    _write_skill(
        skills_dir,
        "builtin/core/bogus_roles",
        "name: bogus_roles\ndescription: Invalid role names\nroles: [banana]\n",
    )
    monkeypatch.setattr(loader_module, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(loader_module, "USER_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(loader_module, "SKILLS_STATE_FILE", tmp_path / "skills_state.json")
    # Cache-first scan: point the snapshot at a missing file so the tmp tree scans.
    monkeypatch.setattr(loader_module, "SKILLS_SNAPSHOT_FILE", tmp_path / "skills_snapshot.json")
    return skills_dir


class TestScanSkillsRoles:
    def test_list_parsed(self, role_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["exec_only"]["roles"] == ["executor"]
        assert skills["research_pair"]["roles"] == ["researcher", "librarian"]

    def test_absent_is_none(self, role_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["untagged"]["roles"] is None

    def test_scalar_string_becomes_one_role(self, role_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["scalar_role"]["roles"] == ["executor"]

    def test_all_invalid_falls_back_to_none(self, role_skills_dir):
        skills = {s["name"]: s for s in scan_skills(use_cache=False)}
        assert skills["bogus_roles"]["roles"] is None

    def test_roles_in_every_entry(self, role_skills_dir):
        for skill in scan_skills(use_cache=False):
            assert "roles" in skill


class TestGetSkillsTextRoles:
    def test_executor_sees_its_skill_and_untagged(self, role_skills_dir):
        xml = get_skills_text(caller_scope="subagent", caller_role="executor")
        assert "exec_only" in xml
        assert "untagged" in xml
        assert "research_pair" not in xml

    def test_researcher_sees_its_skill_not_the_executor_one(self, role_skills_dir):
        xml = get_skills_text(caller_scope="subagent", caller_role="researcher")
        assert "research_pair" in xml
        assert "exec_only" not in xml

    def test_no_caller_role_sees_everything(self, role_skills_dir):
        # main agent / identity GENERAL role
        xml = get_skills_text(caller_scope="main")
        assert "exec_only" in xml
        assert "research_pair" in xml

    def test_scope_still_applies(self, role_skills_dir):
        written = role_skills_dir / "builtin/core/main_vault"
        written.mkdir(parents=True)
        (written / "SKILL.md").write_text(
            "---\nname: main_vault\ndescription: d\nscope: main_only\nroles: [executor]\n---\nbody",
            encoding="utf-8",
        )
        xml = get_skills_text(caller_scope="subagent", caller_role="executor")
        assert "main_vault" not in xml


class TestSkillsCatalogRoles:
    def test_catalog_honours_the_role(self, role_skills_dir):
        names = {entry["name"] for entry in skills_catalog(caller_role="reviewer")}
        assert "untagged" in names
        assert "exec_only" not in names
        assert "research_pair" not in names

    def test_catalog_without_a_role_lists_everything(self, role_skills_dir):
        names = {entry["name"] for entry in skills_catalog()}
        assert "exec_only" in names and "research_pair" in names
