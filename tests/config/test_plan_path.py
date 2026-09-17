"""Unit tests for the plan/boulder path resolver in config/path.py.

The resolver is the single source of truth for plan-file locations after the
``.omo/plans/`` → ``workspace/sessions/<session_id>/plans/`` migration. The
tests build a throwaway repo tree (``ROOT_DIR`` / ``SESSIONS_DIR`` repointed at
``tmp_path``) so no real boulder/plan file is read or written.
"""

from pathlib import Path

import pytest

from config import path as config_path

pytestmark = [pytest.mark.unit]

SESSION = "sess-1"


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Repoint ROOT_DIR/SESSIONS_DIR at a tmp repo tree."""
    root = tmp_path / "repo"
    sessions = root / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(config_path, "ROOT_DIR", root)
    monkeypatch.setattr(config_path, "SESSIONS_DIR", sessions)
    return root, sessions


def _write(path: Path, text: str = "# Plan\n- [ ] step\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestSessionPlansDir:
    def test_safe_session_returns_scoped_dir(self, roots: tuple[Path, Path]) -> None:
        _root, sessions = roots

        assert config_path.session_plans_dir(SESSION) == sessions / SESSION / "plans"

    @pytest.mark.parametrize(
        "unsafe",
        ["", ".", "..", "a/b", "../escape", "/abs/path", "a\\b", "..\\escape"],
    )
    def test_unsafe_session_returns_none(self, roots: tuple[Path, Path], unsafe: str) -> None:
        assert config_path.session_plans_dir(unsafe) is None

    def test_safe_segment_predicate_rejects_absolute_escape(self) -> None:
        assert config_path.is_safe_session_segment("/etc/passwd") is False
        assert config_path.is_safe_session_segment("opencode:ses_abc") is True


class TestResolvePlanPath:
    def test_bare_filename_resolves_in_session_dir(self, roots: tuple[Path, Path]) -> None:
        _root, sessions = roots
        plan = _write(sessions / SESSION / "plans" / "auth.md")

        assert config_path.resolve_plan_path("auth.md", SESSION) == plan

    def test_relative_new_form_resolves_against_root(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        plan = _write(root / "workspace" / "sessions" / SESSION / "plans" / "auth.md")

        assert config_path.resolve_plan_path(f"workspace/sessions/{SESSION}/plans/auth.md") == plan

    def test_absolute_new_form_resolves_when_file_exists(self, roots: tuple[Path, Path]) -> None:
        _root, sessions = roots
        plan = _write(sessions / SESSION / "plans" / "auth.md")

        assert config_path.resolve_plan_path(str(plan)) == plan

    def test_legacy_omo_reference_resolves(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        plan = _write(root / ".omo" / "plans" / "old.md")

        assert config_path.resolve_plan_path(".omo/plans/old.md") == plan

    def test_migrated_file_found_behind_legacy_reference(self, roots: tuple[Path, Path]) -> None:
        _root, sessions = roots
        plan = _write(sessions / SESSION / "plans" / "old.md")

        # No .omo copy exists: the legacy ref must reach the migrated file.
        assert config_path.resolve_plan_path(".omo/plans/old.md", SESSION) == plan

    def test_new_location_wins_over_legacy_copy(self, roots: tuple[Path, Path]) -> None:
        root, sessions = roots
        legacy = _write(root / ".omo" / "plans" / "same.md", "legacy")
        scoped = _write(sessions / SESSION / "plans" / "same.md", "new")

        resolved = config_path.resolve_plan_path(".omo/plans/same.md", SESSION)

        assert resolved == scoped
        assert resolved != legacy

    def test_legacy_basename_fallback_without_session(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        plan = _write(root / ".omo" / "plans" / "bare.md")

        assert config_path.resolve_plan_path("bare.md") == plan

    def test_unsafe_session_id_skips_scoped_candidate(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        plan = _write(root / ".omo" / "plans" / "bare.md")

        assert config_path.resolve_plan_path("bare.md", "../escape") == plan

    @pytest.mark.parametrize("missing", ["nope.md", ".omo/plans/nope.md"])
    def test_missing_file_returns_none(self, roots: tuple[Path, Path], missing: str) -> None:
        assert config_path.resolve_plan_path(missing, SESSION) is None

    def test_missing_absolute_path_returns_none(self, tmp_path: Path) -> None:
        assert config_path.resolve_plan_path(str(tmp_path / "ghost.md")) is None

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_reference_returns_none(self, blank: str | None) -> None:
        assert config_path.resolve_plan_path(blank, SESSION) is None


class TestResolveOrchestrationPaths:
    def test_boulder_path_is_root_absolute(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots

        assert config_path.resolve_boulder_path() == root / ".omo" / "boulder.json"
        assert config_path.resolve_boulder_path().is_absolute()

    def test_evidence_ledger_path_is_root_absolute(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots

        assert config_path.resolve_evidence_ledger_path() == root / ".omo" / "ledger.jsonl"

    def test_start_work_ledger_path_is_root_absolute(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots

        assert (
            config_path.resolve_start_work_ledger_path()
            == root / ".omo" / "start-work" / "ledger.jsonl"
        )
