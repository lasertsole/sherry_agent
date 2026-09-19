"""Unit tests for the plan/boulder/ledger path resolver in config/path.py.

The resolver is the single source of truth for plan-file locations
(``workspace/sessions/<session_id>/plans/``) and for the Sherry-owned
boulder/ledger paths (``src/data/``). The tests build a throwaway repo tree
(``ROOT_DIR`` / ``SESSIONS_DIR`` / ``SRC_DIR`` repointed at ``tmp_path``) so no
real boulder/plan/ledger file is read or written.

The reverse-lock tests pin the contract that no external orchestration
directory participates in plan resolution: a reference that only exists under
the external tree resolves to ``None`` and is never accepted as a fallback.
"""

from pathlib import Path

import pytest

from config import path as config_path

pytestmark = [pytest.mark.unit]

SESSION = "sess-1"

# The external orchestration directory that must never participate in plan
# resolution; the reverse-lock tests below pin that contract.
_FORBIDDEN_DIR = ".omo"


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Repoint ROOT_DIR/SESSIONS_DIR/SRC_DIR at a tmp repo tree."""
    root = tmp_path / "repo"
    sessions = root / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(config_path, "ROOT_DIR", root)
    monkeypatch.setattr(config_path, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(config_path, "SRC_DIR", root / "src")
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

    def test_session_scoped_file_wins_over_repo_relative_copy(
        self, roots: tuple[Path, Path]
    ) -> None:
        root, sessions = roots
        repo_level = _write(root / "plans" / "same.md", "repo")
        scoped = _write(sessions / SESSION / "plans" / "same.md", "scoped")

        resolved = config_path.resolve_plan_path("plans/same.md", SESSION)

        assert resolved == scoped
        assert resolved != repo_level

    def test_reverse_lock_relative_reference_returns_none(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        _write(root / _FORBIDDEN_DIR / "plans" / "old.md")

        assert config_path.resolve_plan_path(f"{_FORBIDDEN_DIR}/plans/old.md") is None
        assert config_path.resolve_plan_path(f"{_FORBIDDEN_DIR}/plans/old.md", SESSION) is None

    def test_reverse_lock_bare_basename_is_not_fallback(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        _write(root / _FORBIDDEN_DIR / "plans" / "bare.md")

        assert config_path.resolve_plan_path("bare.md") is None
        assert config_path.resolve_plan_path("bare.md", SESSION) is None

    def test_hidden_tooling_directory_never_resolves(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        _write(root / ".cache" / "plans" / "x.md")

        assert config_path.resolve_plan_path(".cache/plans/x.md") is None

    def test_upward_traversal_never_resolves(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        outside = _write(root.parent / "outside.md")

        assert config_path.resolve_plan_path("../outside.md") is None
        assert outside.exists()

    def test_unsafe_session_id_falls_through_to_repo_relative(
        self, roots: tuple[Path, Path]
    ) -> None:
        root, _sessions = roots
        plan = _write(root / "bare.md")

        assert config_path.resolve_plan_path("bare.md", "../escape") == plan

    def test_unsafe_session_id_does_not_reach_session_tree(self, roots: tuple[Path, Path]) -> None:
        _root, sessions = roots
        _write(sessions / SESSION / "plans" / "bare.md")

        assert config_path.resolve_plan_path("bare.md", "../escape") is None

    @pytest.mark.parametrize("missing", ["nope.md", f"{_FORBIDDEN_DIR}/plans/nope.md"])
    def test_missing_file_returns_none(self, roots: tuple[Path, Path], missing: str) -> None:
        assert config_path.resolve_plan_path(missing, SESSION) is None

    def test_missing_absolute_path_returns_none(self, tmp_path: Path) -> None:
        assert config_path.resolve_plan_path(str(tmp_path / "ghost.md")) is None

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_reference_returns_none(self, blank: str | None) -> None:
        assert config_path.resolve_plan_path(blank, SESSION) is None


class TestResolveSherryDataPaths:
    def test_boulder_path_is_sherry_owned_and_absolute(self, roots: tuple[Path, Path]) -> None:
        root, _sessions = roots
        boulder = config_path.resolve_boulder_path()

        assert boulder == root / "src" / "data" / "boulder.json"
        assert boulder.is_absolute()
        assert _FORBIDDEN_DIR not in boulder.parts

    def test_boulder_path_absent_means_no_active_work(self, roots: tuple[Path, Path]) -> None:
        assert config_path.resolve_boulder_path().exists() is False

    def test_evidence_ledger_path_is_sherry_owned_and_absolute(
        self, roots: tuple[Path, Path]
    ) -> None:
        root, _sessions = roots
        ledger = config_path.resolve_evidence_ledger_path()

        assert ledger == root / "src" / "data" / "evidence-ledger.jsonl"
        assert _FORBIDDEN_DIR not in ledger.parts

    def test_start_work_ledger_resolver_is_removed(self) -> None:
        assert not hasattr(config_path, "resolve_start_work_ledger_path")
