"""Unit tests for path_utils: resolve_project_path + resolve_external_path.

Covers the test matrix sections A-F:
A. resolve_project_path basics (A1-A5)
B. External safe fast path inside ROOT_DIR (B1-B3)
C. YOLO mechanism (C1-C5)
D. Session allowlist exact matching (D1-D7)
E. Subagent authorization inheritance helpers (E1-E5)
F. HITL interrupt behavior (F1-F5)
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agent.tools.pub_base.path_utils import (
    resolve_project_path,
    resolve_external_path,
    resolve_path,
    PathOutOfBoundsError,
    _is_yolo,
    _is_subagent,
    _candidate_session_ids,
    _add_to_allowlist,
    _extract_session_id,
)
from config import ROOT_DIR

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeDB:
    """In-memory stand-in for state_register_db (same get/set signatures)."""

    def __init__(self):
        self.store: dict[tuple[str, str], object] = {}
        self.set_calls: list[tuple[str, str, object]] = []

    def get_state(self, session_id: str, key: str, default=None):
        return self.store.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value) -> bool:
        self.store[(session_id, key)] = value
        self.set_calls.append((session_id, key, value))
        return True


@pytest.fixture
def clean_state(monkeypatch):
    """Reset state_register_mem to empty and swap state_register_db for a fake."""
    from runtime import state_register_mem
    import runtime

    monkeypatch.setattr(state_register_mem, "_states", {})
    fake_db = _FakeDB()
    monkeypatch.setattr(runtime, "state_register_db", fake_db)
    monkeypatch.setattr(runtime, "state_register_mem", state_register_mem)
    yield fake_db


@pytest.fixture
def mock_interrupt(monkeypatch):
    """Patch langgraph.types.interrupt to return a canned response and record calls."""

    calls: list[object] = []

    def _make(response: dict):
        def _interrupt(value):
            calls.append(value)
            return response

        monkeypatch.setattr("langgraph.types.interrupt", _interrupt)

    _make.calls = calls
    return _make


def _external(tmp_path: Path, name: str) -> str:
    """An absolute path outside ROOT_DIR (tmp_path never lives under the repo)."""
    return str(Path(tmp_path).resolve() / name)


# ── A. resolve_project_path basics ──────────────────────────────────────


class TestResolveProjectPath:
    def test_relative_path_joins_root(self):
        resolved = resolve_project_path("src/main.py")
        assert resolved == (ROOT_DIR / "src/main.py").resolve()

    def test_absolute_path_inside_root(self):
        target = ROOT_DIR / "config.py"
        resolved = resolve_project_path(str(target))
        assert resolved == target.resolve()

    def test_tilde_expansion_inside_root(self, monkeypatch):
        target = ROOT_DIR / "in_root_file.py"
        monkeypatch.setattr(
            "os.path.expanduser", lambda p: str(target) if p == "~/in_root_file.py" else p
        )
        resolved = resolve_project_path("~/in_root_file.py")
        assert resolved == target.resolve()

    def test_absolute_path_outside_root_rejected(self):
        with pytest.raises(PathOutOfBoundsError):
            resolve_project_path("/etc/passwd")

    def test_deprecated_alias_warns_and_resolves(self):
        with pytest.warns(DeprecationWarning):
            resolved = resolve_path("src/main.py")
        assert resolved == (ROOT_DIR / "src/main.py").resolve()


# ── B. External safe fast path ──────────────────────────────────────────


class TestExternalSafeFastPath:
    def test_internal_path_skips_state_checks(self, tmp_path, monkeypatch):
        from runtime import state_register_mem
        import runtime

        mem_get = MagicMock(side_effect=AssertionError("mem consulted for internal path"))
        db_get = MagicMock(side_effect=AssertionError("db consulted for internal path"))
        monkeypatch.setattr(state_register_mem, "get_state", mem_get)
        monkeypatch.setattr(runtime.state_register_db, "get_state", db_get)

        resolved = resolve_external_path("src/main.py", session_id="s1")
        assert resolved == (ROOT_DIR / "src/main.py").resolve()
        mem_get.assert_not_called()
        db_get.assert_not_called()

    def test_root_dir_boundary_returns_directly(self, clean_state):
        resolved = resolve_external_path(str(ROOT_DIR), session_id="s1")
        assert resolved == ROOT_DIR

    def test_root_subdirectory_returns_directly(self, clean_state):
        resolved = resolve_external_path(str(ROOT_DIR / "subdir"), session_id="s1")
        assert resolved == (ROOT_DIR / "subdir").resolve()


# ── C. YOLO mechanism ───────────────────────────────────────────────────


class TestYolo:
    def test_yolo_on_returns_directly(self, tmp_path, clean_state, mock_interrupt):
        clean_state.store[("__global__", "external_path_yolo")] = True
        target = _external(tmp_path, "app.conf")

        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        resolved = resolve_external_path(target, session_id="s1")
        assert resolved == Path(target)
        assert mock_interrupt.calls == [], "YOLO must not raise an approval prompt"

    def test_yolo_off_falls_through_to_interrupt(self, tmp_path, clean_state, mock_interrupt):
        clean_state.store[("__global__", "external_path_yolo")] = False
        target = _external(tmp_path, "app.conf")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolved = resolve_external_path(target, session_id="s1")
        assert resolved == Path(target)
        assert len(mock_interrupt.calls) == 1

    def test_yolo_decision_writes_persistent_flag(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "app.conf")
        mock_interrupt(response={"decisions": [{"type": "yolo"}]})

        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert clean_state.store[("__global__", "external_path_yolo")] is True
        assert clean_state.set_calls == [("__global__", "external_path_yolo", True)]

    def test_yolo_flag_persists_across_restart(self, tmp_path, monkeypatch):
        import sqlite3
        from runtime.state_register import StateRegisterDB

        db_path = tmp_path / "state_register.db"

        def _fresh_db() -> StateRegisterDB:
            db = StateRegisterDB.__new__(StateRegisterDB)
            db.db_path = db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS states (
                        session_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        PRIMARY KEY (session_id, key)
                    )
                    """
                )
                conn.commit()
            return db

        db_first = _fresh_db()
        db_first.set_state("__global__", "external_path_yolo", True)

        import runtime

        monkeypatch.setattr(runtime, "state_register_db", _fresh_db())

        assert _is_yolo() is True

    def test_yolo_allows_other_external_paths_without_prompt(
        self, tmp_path, clean_state, mock_interrupt
    ):
        clean_state.store[("__global__", "external_path_yolo")] = True
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        first = resolve_external_path(_external(tmp_path, "a.txt"), session_id="s1")
        second = resolve_external_path(_external(tmp_path, "b.txt"), session_id="s1")

        assert first == Path(_external(tmp_path, "a.txt"))
        assert second == Path(_external(tmp_path, "b.txt"))
        assert mock_interrupt.calls == []


# ── D. Session allowlist exact matching ─────────────────────────────────


class TestSessionAllowlist:
    def test_empty_allowlist_triggers_interrupt(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "app.conf")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolved = resolve_external_path(target, session_id="s1")
        assert resolved == Path(target)
        assert len(mock_interrupt.calls) == 1

    def test_exact_path_hit_returns_without_interrupt(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "app.conf")
        _add_to_allowlist(Path(target), "s1")
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert mock_interrupt.calls == []

    def test_sibling_file_in_same_dir_misses(self, tmp_path, clean_state, mock_interrupt):
        allowed = _external(tmp_path, "app.conf")
        _add_to_allowlist(Path(allowed), "s1")
        other = _external(tmp_path, "other.conf")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolved = resolve_external_path(other, session_id="s1")

        assert resolved == Path(other)
        assert len(mock_interrupt.calls) == 1, "exact match only — siblings need approval"

    def test_unrelated_path_misses(self, tmp_path, clean_state, mock_interrupt):
        _add_to_allowlist(Path(_external(tmp_path, "app.conf")), "s1")
        unrelated = _external(tmp_path, "other/file")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolved = resolve_external_path(unrelated, session_id="s1")

        assert resolved == Path(unrelated)
        assert len(mock_interrupt.calls) == 1

    def test_approve_adds_exact_path_to_allowlist(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        target = _external(tmp_path, "docs/report.md")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert state_register_mem.get_state("s1", "external_path_allowlist") == [target]

    def test_second_access_after_approve_skips_interrupt(
        self, tmp_path, clean_state, mock_interrupt
    ):
        target = _external(tmp_path, "docs/report.md")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})
        resolve_external_path(target, session_id="s1")

        mock_interrupt(response={"decisions": [{"type": "reject"}]})
        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert len(mock_interrupt.calls) == 1, "second access must not re-prompt"

    def test_interrupt_payload_declares_three_decisions(
        self, tmp_path, clean_state, mock_interrupt
    ):
        target = _external(tmp_path, "app.conf")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolve_external_path(target, session_id="s1", action_desc="read file")

        payload = mock_interrupt.calls[0]
        assert payload["action_requests"][0]["name"] == "external_file_access"
        assert payload["action_requests"][0]["args"] == {"path": target}
        review = payload["review_configs"][0]
        assert review["action_name"] == "external_file_access"
        assert review["allowed_decisions"] == ["approve", "yolo", "reject"]


# ── E. Subagent authorization inheritance ───────────────────────────────


class TestSubagentInheritance:
    def test_child_hits_parent_allowlist(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        target = _external(tmp_path, "app.conf")
        state_register_mem.set_state("child_1", "requester_session_key", "parent_s1")
        state_register_mem.set_state("parent_s1", "external_path_allowlist", [target])
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        resolved = resolve_external_path(target, session_id="child_1")

        assert resolved == Path(target)
        assert mock_interrupt.calls == []

    def test_child_without_authorization_denied(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        target = _external(tmp_path, "secret.conf")
        state_register_mem.set_state("child_1", "requester_session_key", "parent_s1")
        state_register_mem.set_state("child_1", "caller_scope", "subagent")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        with pytest.raises(PathOutOfBoundsError, match="not authorized for subagent"):
            resolve_external_path(target, session_id="child_1")
        assert mock_interrupt.calls == [], "subagents must never self-approve"

    def test_child_allowed_when_yolo_on(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        target = _external(tmp_path, "app.conf")
        state_register_mem.set_state("child_1", "caller_scope", "subagent")
        clean_state.store[("__global__", "external_path_yolo")] = True
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        resolved = resolve_external_path(target, session_id="child_1")

        assert resolved == Path(target)
        assert mock_interrupt.calls == []

    def test_caller_scope_identifies_subagent(self, clean_state):
        from runtime import state_register_mem

        state_register_mem.set_state("child_1", "caller_scope", "subagent")
        state_register_mem.set_state("main_1", "caller_scope", "main")

        assert _is_subagent("child_1") is True
        assert _is_subagent("main_1") is False

    def test_candidate_session_ids_ancestry_order(self, clean_state):
        from runtime import state_register_mem

        state_register_mem.set_state("child_s1", "requester_session_key", "parent_s1")

        ids = _candidate_session_ids("child_s1")

        assert ids == ["child_s1", "parent_s1", "__global__"]


# ── F. HITL interrupt behavior ──────────────────────────────────────────


class TestHitlInterrupt:
    def test_main_agent_approve(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        target = _external(tmp_path, "docs/report.md")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert state_register_mem.get_state("s1", "external_path_allowlist") == [target]

    def test_main_agent_yolo(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "docs/report.md")
        mock_interrupt(response={"decisions": [{"type": "yolo"}]})

        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert clean_state.store[("__global__", "external_path_yolo")] is True

    def test_main_agent_reject(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "docs/report.md")
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        with pytest.raises(PathOutOfBoundsError, match="denied"):
            resolve_external_path(target, session_id="s1")

    def test_empty_resume_payload_denied(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "docs/report.md")
        mock_interrupt(response={})

        with pytest.raises(PathOutOfBoundsError, match="no decision"):
            resolve_external_path(target, session_id="s1")

    def test_unavailable_interrupt_fails_closed(self, tmp_path, clean_state, monkeypatch):
        def _broken_interrupt(value):
            raise RuntimeError("interrupt unavailable")

        monkeypatch.setattr("langgraph.types.interrupt", _broken_interrupt)
        target = _external(tmp_path, "docs/report.md")

        with pytest.raises(RuntimeError):
            resolve_external_path(target, session_id="s1")


# ── Helper coverage ─────────────────────────────────────────────────────


class TestHelpers:
    def test_extract_session_id_reads_configurable(self):
        run_manager = MagicMock()
        run_manager.config = {"configurable": {"session_id": "s42"}}

        assert _extract_session_id(run_manager) == "s42"

    def test_extract_session_id_empty_when_missing(self):
        run_manager = MagicMock()
        run_manager.config = {}

        assert _extract_session_id(run_manager) == ""

    def test_add_to_allowlist_is_idempotent(self, clean_state):
        target = Path("/home/u/docs/report.md")
        _add_to_allowlist(target, "s1")
        _add_to_allowlist(target, "s1")

        from runtime import state_register_mem

        assert state_register_mem.get_state("s1", "external_path_allowlist") == [str(target)]
