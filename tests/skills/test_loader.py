"""TDD tests for audit issue #18 — skills/loader.py lazy-import workaround +
string path check.

Audit findings being pinned:

* ``scan_skills`` did ``from .skills_snapshot import read_skills_snapshot``
  INSIDE the function — a circular-import workaround. Root cause: loader and
  skills_snapshot needed each other's functions. Fix: move the leaf
  ``read_skills_snapshot`` (only needs json + SKILLS_DIR) into ``loader.py``
  so the dependency is one-directional (snapshot → loader) and the in-function
  import disappears.
* ``_is_third_party`` matched the raw substring ``"./skills/plugins/"`` —
  a Path-segment check is the correct semantics (a builtin skill living
  under ``skills/builtin/skills/plugins/`` must NOT be third-party).

Functionality constraints (must not change): scan_skills output (dedup,
sorting, third-party default-inactive + state override), the snapshot cache
path, and both import surfaces (``skills.loader.read_skills_snapshot`` new
home; ``skills.skills_snapshot.read_skills_snapshot`` re-exported for
compatibility).
"""

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from config import is_allowed_skill_path
from skills import loader as loader_mod

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _write_skill(root: Path, rel: str, name: str, description: str = "d") -> None:
    skill_md = root / rel / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody", encoding="utf-8"
    )


@pytest.fixture()
def skills_tree(tmp_path, monkeypatch):
    """A tmp skills tree: 2 builtin + 2 third-party skills, empty state file."""
    root = tmp_path / "repo"
    root.mkdir()
    _write_skill(root, "skills/builtin/core/alpha", "alpha")
    _write_skill(root, "skills/builtin/core/beta", "beta")
    _write_skill(root, "skills/plugins/uploaded_one", "uploaded_one")
    _write_skill(root, "skills/plugins/uploaded_two", "uploaded_two")

    monkeypatch.setattr(loader_mod, "SKILLS_DIR", root / "skills")
    monkeypatch.setattr(loader_mod, "ROOT_DIR", root)
    monkeypatch.setattr(loader_mod, "SKILLS_STATE_FILE", root / "skills_state.json")
    return root


# ---------------------------------------------------------------------------
# Circular import removal
# ---------------------------------------------------------------------------


class TestLazyImportRemoval:
    def test_read_skills_snapshot_lives_on_loader_module(self):
        """The moved leaf function must be a module-level attribute of loader.

        Old code imported it inside scan_skills (the audit's workaround) — the
        module attribute did not exist.
        """
        assert hasattr(loader_mod, "read_skills_snapshot")

    def test_scan_skills_has_no_in_function_snapshot_import(self):
        """Structural pin: scan_skills must not carry in-function imports."""
        source = inspect.getsource(loader_mod.scan_skills)
        offenders = [
            line for line in source.splitlines() if line.strip().startswith(("from ", "import "))
        ]
        assert not offenders, (
            f"scan_skills still carries in-function imports (circular-import workaround): {offenders}"
        )

    def test_snapshot_reexports_read_skills_snapshot(self):
        """Compatibility: the old import surface must keep working."""
        from skills import skills_snapshot

        assert skills_snapshot.read_skills_snapshot is loader_mod.read_skills_snapshot

    def test_import_works_in_both_orders(self):
        """Fresh interpreter: either module can be imported first."""
        code = (
            "import skills.skills_snapshot as ss\n"
            "import skills.loader as lo\n"
            "assert ss.read_skills_snapshot is lo.read_skills_snapshot\n"
            "print('ORDER_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"import cycle:\n{result.stdout}\n{result.stderr}"
        assert "ORDER_OK" in result.stdout

        reversed_code = (
            "import skills.loader as lo\n"
            "import skills.skills_snapshot as ss\n"
            "assert ss.read_skills_snapshot is lo.read_skills_snapshot\n"
            "print('ORDER_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", reversed_code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"import cycle:\n{result.stdout}\n{result.stderr}"
        assert "ORDER_OK" in result.stdout


# ---------------------------------------------------------------------------
# _is_third_party: Path-segment semantics
# ---------------------------------------------------------------------------


class TestIsThirdParty:
    def test_plugins_location_is_third_party(self):
        assert loader_mod._is_third_party("./skills/plugins/uploaded_one/SKILL.md") is True

    def test_builtin_and_auto_are_not_third_party(self):
        assert loader_mod._is_third_party("./skills/builtin/core/alpha/SKILL.md") is False
        assert loader_mod._is_third_party("./skills/auto/learned/SKILL.md") is False

    def test_nested_lookalike_is_not_third_party(self):
        """A builtin path containing 'skills/plugins' as a SUBSTRING is not
        third-party — the old substring check misclassified it."""
        location = "./skills/builtin/skills/plugins/foo/SKILL.md"
        assert loader_mod._is_third_party(location) is False

    def test_bare_plugins_dir_is_third_party(self):
        assert loader_mod._is_third_party("./skills/plugins") is True


# ---------------------------------------------------------------------------
# scan_skills: original behavior unchanged
# ---------------------------------------------------------------------------


class TestScanSkillsBehavior:
    def test_builtin_active_third_party_inactive_by_default(self, skills_tree):
        skills = {s["name"]: s for s in loader_mod.scan_skills(use_cache=False)}

        assert set(skills) == {"alpha", "beta", "uploaded_one", "uploaded_two"}
        assert skills["alpha"]["active"] is True
        assert skills["beta"]["active"] is True
        assert skills["uploaded_one"]["active"] is False
        assert skills["uploaded_two"]["active"] is False
        assert skills["uploaded_one"]["location"] == "./skills/plugins/uploaded_one/SKILL.md"
        assert skills["alpha"]["location"] == "./skills/builtin/core/alpha/SKILL.md"

    def test_state_file_activates_third_party(self, skills_tree, monkeypatch):
        state_file = skills_tree / "skills_state.json"
        state_file.write_text(json.dumps({"uploaded_one": {"active": True}}), encoding="utf-8")

        skills = {s["name"]: s for s in loader_mod.scan_skills(use_cache=False)}

        assert skills["uploaded_one"]["active"] is True
        assert skills["uploaded_two"]["active"] is False

    def test_sorted_by_name(self, skills_tree):
        names = [s["name"] for s in loader_mod.scan_skills(use_cache=False)]
        assert names == sorted(names)

    def test_cache_hit_returns_snapshot_without_scan(self, skills_tree, monkeypatch):
        """use_cache=True with a snapshot file returns its content verbatim."""
        snapshot_file = skills_tree / "skills" / "skills_snapshot.json"
        sentinel = [
            {
                "name": "cached",
                "description": "",
                "location": "./skills/builtin/cached/SKILL.md",
                "scope": "all",
                "active": True,
            }
        ]
        snapshot_file.write_text(json.dumps(sentinel), encoding="utf-8")

        monkeypatch.setattr(loader_mod, "read_skills_snapshot", lambda: sentinel)

        assert loader_mod.scan_skills(use_cache=True) == sentinel

    def test_use_cache_false_skips_cache_read(self, skills_tree, monkeypatch):
        """use_cache=False must not even read the snapshot file (legacy behavior)."""
        snapshot_file = skills_tree / "skills" / "skills_snapshot.json"
        snapshot_file.write_text(json.dumps([{"name": "stale"}]), encoding="utf-8")

        called = {}

        def _spy():
            called["read"] = True
            return [{"name": "stale"}]

        monkeypatch.setattr(loader_mod, "read_skills_snapshot", _spy)

        skills = loader_mod.scan_skills(use_cache=False)

        assert called == {}, "use_cache=False must skip the cache read entirely"
        assert {s["name"] for s in skills} == {"alpha", "beta", "uploaded_one", "uploaded_two"}


# ---------------------------------------------------------------------------
# read_skills_snapshot (moved leaf): file behavior
# ---------------------------------------------------------------------------


class TestReadSkillsSnapshot:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader_mod, "SKILLS_DIR", tmp_path / "skills")
        assert loader_mod.read_skills_snapshot() is None

    def test_existing_file_returns_content(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True)
        payload = [{"name": "alpha", "location": "./skills/builtin/alpha/SKILL.md"}]
        (skills_dir / "skills_snapshot.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(loader_mod, "SKILLS_DIR", skills_dir)

        assert loader_mod.read_skills_snapshot() == payload


# ---------------------------------------------------------------------------
# Discovery restricted to builtin/auto/plugins
# ---------------------------------------------------------------------------


class TestAllowedSkillRoots:
    def _tree(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.setattr(loader_mod, "SKILLS_DIR", root / "skills")
        monkeypatch.setattr(loader_mod, "ROOT_DIR", root)
        monkeypatch.setattr(loader_mod, "SKILLS_STATE_FILE", root / "skills_state.json")
        return root

    def test_allowed_roots_discovered(self, tmp_path, monkeypatch):
        root = self._tree(tmp_path, monkeypatch)
        _write_skill(root, "skills/builtin/core/alpha", "alpha")
        _write_skill(root, "skills/auto/learned/auto_one", "auto_one")
        _write_skill(root, "skills/plugins/up/plug_one", "plug_one")

        names = {s["name"] for s in loader_mod.scan_skills(use_cache=False)}

        assert {"alpha", "auto_one", "plug_one"} <= names

    def test_skill_directly_under_skills_ignored(self, tmp_path, monkeypatch):
        root = self._tree(tmp_path, monkeypatch)
        _write_skill(root, "skills", "stray")
        _write_skill(root, "skills/builtin/core/alpha", "alpha")

        names = {s["name"] for s in loader_mod.scan_skills(use_cache=False)}

        assert "stray" not in names
        assert "alpha" in names

    def test_unexpected_subdir_ignored(self, tmp_path, monkeypatch):
        root = self._tree(tmp_path, monkeypatch)
        _write_skill(root, "skills/misc/stray", "stray")

        names = {s["name"] for s in loader_mod.scan_skills(use_cache=False)}

        assert "stray" not in names

    def test_is_allowed_skill_path(self, tmp_path):
        skills_dir = tmp_path / "skills"
        assert is_allowed_skill_path(skills_dir / "builtin/x/SKILL.md", skills_dir) is True
        assert is_allowed_skill_path(skills_dir / "auto/x/SKILL.md", skills_dir) is True
        assert is_allowed_skill_path(skills_dir / "plugins/x/SKILL.md", skills_dir) is True
        assert is_allowed_skill_path(skills_dir / "SKILL.md", skills_dir) is False
        assert is_allowed_skill_path(skills_dir / "misc/x/SKILL.md", skills_dir) is False
        assert is_allowed_skill_path(tmp_path / "outside/SKILL.md", skills_dir) is False
