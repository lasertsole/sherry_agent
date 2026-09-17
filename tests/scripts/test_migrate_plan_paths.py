"""Unit tests for the one-shot plan-path migration script.

The script is loaded from its file path (``scripts/`` is not a package). Every
test runs against a throwaway repo tree under ``tmp_path`` — the real ``.omo``
directory is never read or written.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit]

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "migrate_plan_paths.py"


@pytest.fixture(scope="module")
def migrator() -> ModuleType:
    """Load the migration script from its file path."""
    spec = importlib.util.spec_from_file_location("migrate_plan_paths", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".omo" / "plans").mkdir(parents=True)
    return root


def _boulder(
    root: Path, *, works: dict, active_plan: str, active_work_id: str | None = None
) -> Path:
    data = {
        "schema_version": 2,
        "active_work_id": active_work_id,
        "works": works,
        "active_plan": active_plan,
    }
    return _write(root / ".omo" / "boulder.json", json.dumps(data, indent=2) + "\n")


def _read_boulder(root: Path) -> dict:
    return json.loads((root / ".omo" / "boulder.json").read_text(encoding="utf-8"))


def test_dry_run_stages_without_touching_disk(migrator: ModuleType, repo: Path) -> None:
    source = _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    _boulder(
        repo,
        works={
            "w1": {"status": "active", "active_plan": ".omo/plans/a.md", "session_ids": ["sid-a"]}
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )
    boulder_before = (repo / ".omo" / "boulder.json").read_text(encoding="utf-8")

    report = migrator.migrate(repo, apply=False)

    assert report.errors == []
    assert report.moves == [
        (source.as_posix(), (repo / "workspace/sessions/sid-a/plans/a.md").as_posix())
    ]
    assert source.is_file()
    assert not (repo / "workspace/sessions/sid-a/plans/a.md").exists()
    assert (repo / ".omo" / "boulder.json").read_text(encoding="utf-8") == boulder_before


def test_apply_moves_files_and_rewrites_boulder(migrator: ModuleType, repo: Path) -> None:
    _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    _boulder(
        repo,
        works={
            "w1": {"status": "active", "active_plan": ".omo/plans/a.md", "session_ids": ["sid-a"]}
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )

    report = migrator.migrate(repo, apply=True)

    assert report.errors == []
    dest = repo / "workspace/sessions/sid-a/plans/a.md"
    assert dest.read_text(encoding="utf-8") == "# A\n"
    assert not (repo / ".omo" / "plans" / "a.md").exists()
    boulder = _read_boulder(repo)
    assert boulder["works"]["w1"]["active_plan"] == "workspace/sessions/sid-a/plans/a.md"
    assert boulder["active_plan"] == "workspace/sessions/sid-a/plans/a.md"
    assert any(change[0] == "active_plan" for change in report.boulder_changes)


def test_derive_session_id_fallback_chain(migrator: ModuleType) -> None:
    assert migrator.derive_session_id({"session_ids": ["sid-1", "sid-2"]}) == "sid-1"
    assert migrator.derive_session_id({"session_origins": {"sid-o": "appended"}}) == "sid-o"
    assert migrator.derive_session_id({"task_sessions": {"t1": {"session_id": "sid-t"}}}) == "sid-t"
    assert migrator.derive_session_id({}) is None


def test_task_sessions_without_session_id_is_not_derivable(migrator: ModuleType) -> None:
    assert migrator.derive_session_id({"task_sessions": {"t1": {"status": "completed"}}}) is None


def test_unmapped_plan_is_reported_and_left_in_place(migrator: ModuleType, repo: Path) -> None:
    _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    orphan = _write(repo / ".omo" / "plans" / "orphan.md", "# Orphan\n")
    _boulder(
        repo,
        works={
            "w1": {"status": "active", "active_plan": ".omo/plans/a.md", "session_ids": ["sid-a"]}
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )

    report = migrator.migrate(repo, apply=True)

    assert report.unmapped == [".omo/plans/orphan.md"]
    assert orphan.is_file()
    assert not (repo / "workspace/sessions/sid-a/plans/orphan.md").exists()


def test_missing_session_reference_errors_and_writes_nothing(
    migrator: ModuleType, repo: Path
) -> None:
    source = _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    _boulder(
        repo,
        works={"w1": {"status": "active", "active_plan": ".omo/plans/a.md"}},
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )
    boulder_before = (repo / ".omo" / "boulder.json").read_text(encoding="utf-8")

    report = migrator.migrate(repo, apply=True)

    assert report.errors and "refusing to guess" in report.errors[0]
    assert source.is_file()
    assert (repo / ".omo" / "boulder.json").read_text(encoding="utf-8") == boulder_before


def test_missing_source_errors(migrator: ModuleType, repo: Path) -> None:
    _boulder(
        repo,
        works={
            "w1": {
                "status": "active",
                "active_plan": ".omo/plans/gone.md",
                "session_ids": ["sid-a"],
            }
        },
        active_plan=".omo/plans/gone.md",
        active_work_id="w1",
    )

    report = migrator.migrate(repo, apply=True)

    assert any("source missing" in error for error in report.errors)


def test_unsafe_session_id_errors(migrator: ModuleType, repo: Path) -> None:
    _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    _boulder(
        repo,
        works={
            "w1": {
                "status": "active",
                "active_plan": ".omo/plans/a.md",
                "session_ids": ["../escape"],
            }
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )

    report = migrator.migrate(repo, apply=True)

    assert any("unsafe session id" in error for error in report.errors)


def test_already_migrated_file_rewrites_boulder_only(migrator: ModuleType, repo: Path) -> None:
    dest = _write(repo / "workspace" / "sessions" / "sid-a" / "plans" / "a.md", "# A\n")
    _boulder(
        repo,
        works={
            "w1": {"status": "active", "active_plan": ".omo/plans/a.md", "session_ids": ["sid-a"]}
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )

    report = migrator.migrate(repo, apply=True)

    assert report.moves == []
    assert report.already == [((repo / ".omo" / "plans" / "a.md").as_posix(), dest.as_posix())]
    assert _read_boulder(repo)["works"]["w1"]["active_plan"] == (
        "workspace/sessions/sid-a/plans/a.md"
    )


def test_conflicting_destination_errors(migrator: ModuleType, repo: Path) -> None:
    _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    _write(repo / "workspace" / "sessions" / "sid-a" / "plans" / "a.md", "# DIFFERENT\n")
    _boulder(
        repo,
        works={
            "w1": {"status": "active", "active_plan": ".omo/plans/a.md", "session_ids": ["sid-a"]}
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )

    report = migrator.migrate(repo, apply=True)

    assert any("destination differs" in error for error in report.errors)


def test_main_exit_codes(migrator: ModuleType, repo: Path) -> None:
    _write(repo / ".omo" / "plans" / "a.md", "# A\n")
    _boulder(
        repo,
        works={
            "w1": {"status": "active", "active_plan": ".omo/plans/a.md", "session_ids": ["sid-a"]}
        },
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )

    assert migrator.main(["--root", str(repo)]) == 0

    _boulder(
        repo,
        works={"w1": {"status": "active", "active_plan": ".omo/plans/a.md"}},
        active_plan=".omo/plans/a.md",
        active_work_id="w1",
    )
    assert migrator.main(["--root", str(repo), "--apply"]) == 1
