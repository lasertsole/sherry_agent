"""Unit tests for the clawhub post-install security scan gate.

These tests exercise ``clawhub_runner._scan_plugin_skills`` — the function that
runs after a successful ``clawhub install/update`` and rolls back any skill
flagged ``DO_NOT_INSTALL`` by the SkillSpector scanner.

We stub the lazy ``server.service.skill_scanner`` import (the module may be
unavailable in the agent/skills context) and monkeypatch the clawhub module's
``PLUGIN_SKILLS_DIR`` / ``SKILLS_STATE_FILE`` globals to tmp dirs so the tests
never touch the real ``skills/plugins/`` tree.

Covered scenarios:
* DO_NOT_INSTALL skill is removed from disk AND its state entry pruned.
* CAUTION skill is kept.
* SAFE skill is kept.
* Scanner unavailable (UNAVAILABLE) -> kept (fail-open).
* scan_skill raising -> kept (fail-open).
* Missing plugins dir -> empty scan.
* ImportError on the scanner module -> fail-open, no crash.
* Orphaned state entries are pruned after a rollback.
* Summary counters are accurate.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skills.builtin.core.clawhub.scripts import clawhub_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_scan_result(recommendation: str | None = "SAFE"):
    """Build a no-backend ScanResult-like stub with the predicate properties."""
    return SimpleNamespace(
        is_unavailable=False,
        is_do_not_install=(recommendation == "DO_NOT_INSTALL"),
        is_caution=(recommendation == "CAUTION"),
        risk_score=50,
        risk_recommendation=recommendation,
    )


def _make_skill(plugins_dir: Path, name: str) -> Path:
    """Create a skill dir ``plugins_dir/<name>/SKILL.md`` and return the path."""
    skill_root = plugins_dir / name
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n\nbody\n", encoding="utf-8"
    )
    return skill_root


def _write_state(state_file: Path, state: dict[str, object]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    import json

    state_file.write_text(json.dumps(state), encoding="utf-8")


def _patch_scanner(scan_results=None, raise_on_call=None, import_error=False):
    """Patch clawhub_runner's lazy ``server.service.skill_scanner`` import.

    scan_results: a callable(path)->ScanResult stub, or a fixed result.
    raise_on_call: optional exception to raise from scan_skill.
    import_error: if True, force the ImportError fail-open branch.
    """
    if import_error:
        # Remove the module from sys.modules so the lazy `from
        # server.service.skill_scanner import ...` raises ImportError.
        return patch.dict(
            "sys.modules",
            {"server.service.skill_scanner": None},
            clear=False,
        )
    if callable(scan_results):
        scan_fn = scan_results
    else:
        scan_fn = lambda path: scan_results  # noqa: E731

    def fake_scan_skill(path):
        if raise_on_call is not None:
            raise raise_on_call
        return scan_fn(path)

    scanner_mod = SimpleNamespace(
        scan_skill=fake_scan_skill,
        build_reject_message=lambda r: (
            "Skill rejected by security scanner" if r.is_do_not_install else None
        ),
    )
    return patch.dict(
        "sys.modules",
        {"server.service.skill_scanner": scanner_mod},
    )


@pytest.fixture
def clamp_globals(tmp_path):
    """Point clawhub_runner's plugin dir + state file at tmp_path."""
    plugins_dir = tmp_path / "plugins"
    state_file = tmp_path / "plugins" / ".state.json"
    with patch.object(clawhub_runner, "PLUGIN_SKILLS_DIR", plugins_dir), patch.object(
        clawhub_runner, "SKILLS_STATE_FILE", state_file
    ):
        yield plugins_dir, state_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScanPluginSkills:
    def test_do_not_install_rolled_back_and_state_pruned(self, clamp_globals):
        plugins_dir, state_file = clamp_globals
        _make_skill(plugins_dir, "evil_skill")
        _make_skill(plugins_dir, "other")  # exists on disk -> state entry must survive
        _write_state(state_file, {"evil_skill": {"active": True}, "other": {"active": False}})

        def path_sensitive(path: Path):
            if path.name == "evil_skill":
                return _fake_scan_result("DO_NOT_INSTALL")
            return _fake_scan_result("SAFE")

        with _patch_scanner(path_sensitive):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 2
        assert summary["rolled_back"] == 1
        assert not (plugins_dir / "evil_skill").exists()

        import json

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert "evil_skill" not in state  # pruned
        assert "other" in state  # untouched

    def test_caution_kept_and_counted(self, clamp_globals):
        plugins_dir, state_file = clamp_globals
        _make_skill(plugins_dir, "risky_skill")
        with _patch_scanner(_fake_scan_result("CAUTION")):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 1
        assert summary["caution"] == 1
        assert summary["rolled_back"] == 0
        assert (plugins_dir / "risky_skill").is_dir()  # kept

    def test_safe_kept(self, clamp_globals):
        plugins_dir, _state_file = clamp_globals
        _make_skill(plugins_dir, "good_skill")
        with _patch_scanner(_fake_scan_result("SAFE")):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 1
        assert summary["rolled_back"] == 0
        assert summary["caution"] == 0
        assert (plugins_dir / "good_skill").is_dir()

    def test_unavailable_fails_open_and_keeps_skill(self, clamp_globals):
        plugins_dir, _state_file = clamp_globals
        _make_skill(plugins_dir, "inconclusive_skill")
        unavailable = SimpleNamespace(
            is_unavailable=True,
            is_do_not_install=False,
            is_caution=False,
            risk_score=None,
            risk_recommendation=None,
        )
        with _patch_scanner(unavailable):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 0
        assert summary["skipped"] == 1
        assert (plugins_dir / "inconclusive_skill").is_dir()

    def test_scan_error_fails_open(self, clamp_globals):
        plugins_dir, _state_file = clamp_globals
        _make_skill(plugins_dir, "boom_skill")
        with _patch_scanner(None, raise_on_call=RuntimeError("scanner exploded")):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 0
        assert summary["skipped"] == 1
        assert (plugins_dir / "boom_skill").is_dir()

    def test_missing_plugins_dir_returns_empty(self, clamp_globals):
        plugins_dir, _state_file = clamp_globals
        # plugins_dir does not exist yet.
        with _patch_scanner(_fake_scan_result("SAFE")):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary == {"scanned": 0, "rolled_back": 0, "caution": 0, "skipped": 0}

    def test_import_error_fails_open(self, clamp_globals):
        plugins_dir, _state_file = clamp_globals
        _make_skill(plugins_dir, "anything")
        with _patch_scanner(import_error=True):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary == {"scanned": 0, "rolled_back": 0, "caution": 0, "skipped": 0}
        assert (plugins_dir / "anything").is_dir()

    def test_author_scoped_skill_root_resolved(self, clamp_globals):
        # clawhub install can create <author>/<slug>/SKILL.md; the skill root
        # must still be the top-level dir under plugins/. A rollback of that
        # top-level author dir must be attempted.
        plugins_dir, state_file = clamp_globals
        author_slug = plugins_dir / "some_author" / "nested_skill"
        author_slug.mkdir(parents=True)
        (author_slug / "SKILL.md").write_text("meta", encoding="utf-8")
        _write_state(state_file, {"some_author": {"active": True}})

        with _patch_scanner(_fake_scan_result("DO_NOT_INSTALL")):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 1
        assert summary["rolled_back"] == 1
        assert not (plugins_dir / "some_author").exists()

        import json

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert "some_author" not in state

    def test_state_prune_after_rollback_removes_orphans(self, clamp_globals):
        plugins_dir, state_file = clamp_globals
        _make_skill(plugins_dir, "bad1")
        _make_skill(plugins_dir, "bad2")
        _write_state(
            state_file,
            {
                "bad1": {"active": True},
                "bad2": {"active": True},
                "ghost": {"active": True},  # no dir on disk either
            },
        )

        res = {"idx": 0}

        def seq(path):
            recs = ["DO_NOT_INSTALL", "DO_NOT_INSTALL"]
            n = res["idx"]
            res["idx"] += 1
            return _fake_scan_result(recs[min(n, 1)])

        with _patch_scanner(seq):
            summary = clawhub_runner._scan_plugin_skills()

        assert summary["scanned"] == 2
        assert summary["rolled_back"] == 2

        import json

        state = json.loads(state_file.read_text(encoding="utf-8"))
        # All rolled-back dirs are gone AND orphaned state keys were pruned.
        assert set(state.keys()) == set()
