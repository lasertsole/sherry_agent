"""Unit tests for path_utils: resolve_project_path + resolve_external_path.

Covers the test matrix sections A-H:
A. resolve_project_path basics (A1-A5)
B. External safe fast path inside ROOT_DIR (B1-B3)
C. YOLO mechanism (C1-C5)
D. Session allowlist matching (D1-D7)
E. Subagent authorization inheritance helpers (E1-E5)
F. HITL interrupt behavior (F1-F5)
G. YOLO deny list: always enforced, beats YOLO / allowlist / subagent (G1-G5)
H. Directory-level allowlist + approve_dir decision (H1-H4)
Three-gate resolution: traversal strings, symlink loops, O_NOFOLLOW
Model-visible path rendering: to_virtual_path / display_path / safe_error_detail
"""

import errno
import os

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agent.tools.pub_base.path_utils import (
    resolve_project_path,
    resolve_external_path,
    resolve_path,
    to_virtual_path,
    display_path,
    safe_error_detail,
    PathOutOfBoundsError,
    _get_yolo_deny_paths,
    _is_eloop_oserror,
    _is_symlink_loop_error,
    _is_yolo,
    _is_yolo_denied,
    _is_subagent,
    _open_no_follow,
    _raise_if_symlink_loop,
    _candidate_session_ids,
    _add_to_allowlist,
    _extract_session_id,
)
from config import ROOT_DIR
from config.features import HITL_DEFAULTS

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

    def test_tilde_path_rejected(self):
        with pytest.raises(PathOutOfBoundsError, match="not allowed"):
            resolve_project_path("~/in_root_file.py")

    def test_absolute_path_outside_root_rejected(self):
        with pytest.raises(PathOutOfBoundsError):
            resolve_project_path("/etc/passwd")

    def test_deprecated_alias_warns_and_resolves(self):
        with pytest.warns(DeprecationWarning):
            resolved = resolve_path("src/main.py")
        assert resolved == (ROOT_DIR / "src/main.py").resolve()


# ── Traversal, symlink-loop and O_NOFOLLOW gates ───────────────────────


class TestResolveGates:
    def test_double_dot_rejected_without_filesystem_access(self, monkeypatch):
        def _boom(self):
            raise AssertionError("resolve() must not run for traversal input")

        monkeypatch.setattr(Path, "resolve", _boom)

        with pytest.raises(PathOutOfBoundsError, match="traversal"):
            resolve_project_path("../../etc/passwd")

    def test_mid_path_traversal_rejected(self):
        with pytest.raises(PathOutOfBoundsError, match="traversal"):
            resolve_project_path("src/../../etc/passwd")

    def test_dotdot_like_names_allowed(self):
        assert resolve_project_path("foo..bar") == (ROOT_DIR / "foo..bar").resolve()
        assert resolve_project_path("配置..md") == (ROOT_DIR / "配置..md").resolve()

    def test_symlink_loop_detected(self, tmp_path, monkeypatch):
        root = tmp_path.resolve()
        monkeypatch.setattr("agent.tools.pub_base.path_utils.ROOT_DIR", root)
        (root / "a").symlink_to(root / "b")
        (root / "b").symlink_to(root / "a")

        with pytest.raises(OSError) as excinfo:
            resolve_project_path("a")

        assert _is_eloop_oserror(excinfo.value)


class TestSymlinkLoopHelpers:
    def test_is_eloop_oserror_matches_eloop(self):
        assert _is_eloop_oserror(OSError(errno.ELOOP, "loop")) is True

    def test_is_eloop_oserror_rejects_other_errno(self):
        assert _is_eloop_oserror(OSError(errno.ENOENT, "missing")) is False
        assert _is_eloop_oserror(None) is False

    def test_is_eloop_oserror_matches_winerror(self):
        exc = OSError(0, "windows loop")
        exc.winerror = 1921
        assert _is_eloop_oserror(exc) is True

    def test_is_symlink_loop_error_follows_chained_cause(self):
        exc = RuntimeError("wrapped")
        exc.__cause__ = OSError(errno.ELOOP, "loop")
        assert _is_symlink_loop_error(exc) is True

    def test_is_symlink_loop_error_plain_runtime(self):
        assert _is_symlink_loop_error(RuntimeError("nope")) is False

    def test_raise_if_symlink_loop_noop_for_regular_file(self, tmp_path):
        regular = tmp_path / "plain.txt"
        regular.write_text("x", encoding="utf-8")

        _raise_if_symlink_loop(regular)

    def test_raise_if_symlink_loop_raises_for_loop(self, tmp_path):
        (tmp_path / "a").symlink_to(tmp_path / "b")
        (tmp_path / "b").symlink_to(tmp_path / "a")

        with pytest.raises(OSError) as excinfo:
            _raise_if_symlink_loop(tmp_path / "a")

        assert excinfo.value.errno == errno.ELOOP


class TestOpenNoFollow:
    def test_rejects_symlink_final_component(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("data", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        with pytest.raises(OSError) as excinfo:
            _open_no_follow(link, os.O_RDONLY)

        assert excinfo.value.errno == errno.ELOOP

    def test_reads_regular_file(self, tmp_path):
        target = tmp_path / "plain.txt"
        target.write_text("plain-data", encoding="utf-8")

        fd = _open_no_follow(target, os.O_RDONLY)
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                fd = -1
                assert f.read() == "plain-data"
        finally:
            if fd >= 0:
                os.close(fd)

    def test_windows_fallback_uses_is_symlink_check(self, tmp_path, monkeypatch):
        target = tmp_path / "target.txt"
        target.write_text("data", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        with pytest.raises(OSError) as excinfo:
            _open_no_follow(link, os.O_RDONLY)
        assert excinfo.value.errno == errno.ELOOP

        fd = _open_no_follow(target, os.O_RDONLY)
        os.close(fd)


# ── Model-visible path rendering ────────────────────────────────────────


class TestPathRendering:
    def test_display_path_normal(self):
        assert display_path(ROOT_DIR / "src" / "main.py") == "/src/main.py"

    def test_display_path_outside_root_falls_back_to_name(self):
        rendered = display_path(Path("/etc/passwd"))

        assert rendered == "passwd"
        assert str(ROOT_DIR) not in rendered

    def test_to_virtual_path_rejects_outside_root(self):
        with pytest.raises(ValueError):
            to_virtual_path(Path("/etc/passwd"))

    def test_to_virtual_path_renders_inside_root(self):
        assert to_virtual_path(ROOT_DIR / "nested" / "file.txt") == "/nested/file.txt"

    def test_safe_detail_strips_embedded_path(self):
        exc = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "/home/user/.env")

        detail = safe_error_detail(exc)

        assert detail == "FileNotFoundError: No such file or directory"
        assert "/home/user/.env" not in detail

    def test_safe_error_detail_uses_reason_when_present(self):
        exc = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        assert safe_error_detail(exc) == "UnicodeDecodeError: invalid start byte"

    def test_safe_error_detail_without_detail_returns_type_name(self):
        assert safe_error_detail(ValueError()) == "ValueError"

    def test_safe_error_detail_drops_generic_path_bearing_message(self):
        # Reference `_safe_detail` (`virtual_mode=True`) never falls back to
        # `str(exc)`: generic exception text (e.g. from `Path.rglob`) can embed
        # the real root path, so only the type name may survive.
        exc = RuntimeError(f"cannot scan {ROOT_DIR}/workspace")

        detail = safe_error_detail(exc)

        assert detail == "RuntimeError"
        assert str(ROOT_DIR) not in detail


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
        from runtime.session.state_register import StateRegisterDB

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


# ── G. YOLO deny list (security floor) ──────────────────────────────────


class TestYoloDenyList:
    def test_yolo_on_denied_path_rejected_without_prompt(self, clean_state, mock_interrupt):
        clean_state.store[("__global__", "external_path_yolo")] = True
        target = str(Path.home() / ".ssh" / "id_rsa")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        with pytest.raises(PathOutOfBoundsError, match="deny list"):
            resolve_external_path(target, session_id="s1")

        assert mock_interrupt.calls == [], "deny list must reject before any HITL prompt"

    def test_yolo_on_non_denied_path_allowed(self, tmp_path, clean_state, mock_interrupt):
        clean_state.store[("__global__", "external_path_yolo")] = True
        target = _external(tmp_path, "safe.txt")
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        assert resolve_external_path(target, session_id="s1") == Path(target)
        assert mock_interrupt.calls == []

    @pytest.mark.parametrize("state", ["yolo", "allowlist", "subagent_inherited"])
    def test_ssh_key_denied_in_every_state(self, state, clean_state, mock_interrupt):
        from runtime import state_register_mem

        target = str(Path.home() / ".ssh" / "id_rsa")
        if state == "yolo":
            clean_state.store[("__global__", "external_path_yolo")] = True
        elif state == "allowlist":
            _add_to_allowlist(Path(target), "s1")
        else:
            _add_to_allowlist(Path(target), "parent_s1")
            state_register_mem.set_state("child_1", "requester_session_key", "parent_s1")
        session_id = "child_1" if state == "subagent_inherited" else "s1"
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        with pytest.raises(PathOutOfBoundsError, match="deny list"):
            resolve_external_path(target, session_id=session_id)

        assert mock_interrupt.calls == []

    def test_sherry_custom_path_denied_even_with_allowlist_hit(
        self, tmp_path, clean_state, mock_interrupt, monkeypatch
    ):
        secret_dir = tmp_path.resolve() / "secrets"
        target = str(secret_dir / "credentials.json")
        _add_to_allowlist(Path(target), "s1")
        monkeypatch.setattr(
            "agent.tools.pub_base.path_utils._get_yolo_deny_paths",
            lambda: [str(secret_dir) + "/"],
        )
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        with pytest.raises(PathOutOfBoundsError, match="deny list"):
            resolve_external_path(target, session_id="s1")

        assert mock_interrupt.calls == []

    def test_entry_without_trailing_separator_is_exact_match(
        self, tmp_path, clean_state, mock_interrupt, monkeypatch
    ):
        deny_file = tmp_path.resolve() / "credentials.json"
        nested = str(tmp_path.resolve() / "nested" / "credentials.json")
        monkeypatch.setattr(
            "agent.tools.pub_base.path_utils._get_yolo_deny_paths",
            lambda: [str(deny_file)],
        )
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        with pytest.raises(PathOutOfBoundsError, match="deny list"):
            resolve_external_path(str(deny_file), session_id="s1")
        assert resolve_external_path(nested, session_id="s1") == Path(nested)


class TestYoloDenyConfig:
    def test_sherry_jsonc_custom_paths_merge_with_defaults(self, tmp_path, monkeypatch):
        sherry = tmp_path / "sherry.jsonc"
        sherry.write_text(
            '{"yolo_deny_paths": ["~/custom-secrets/", "~/.ssh/"]}',
            encoding="utf-8",
        )
        monkeypatch.setattr("config.sherry_settings.SHERRY_CONFIG_PATH", sherry)

        merged = _get_yolo_deny_paths()

        defaults = list(HITL_DEFAULTS["yolo_deny_paths"])
        assert merged[: len(defaults)] == defaults
        assert "~/custom-secrets/" in merged
        assert merged.count("~/.ssh/") == 1, "duplicate user entry must be dropped"

    def test_custom_deny_path_rejects_via_sherry_jsonc(
        self, tmp_path, clean_state, mock_interrupt, monkeypatch
    ):
        secret_dir = tmp_path.resolve() / "custom-secrets"
        sherry = tmp_path / "sherry.jsonc"
        sherry.write_text(
            f'{{"yolo_deny_paths": ["{secret_dir}/"]}}',
            encoding="utf-8",
        )
        monkeypatch.setattr("config.sherry_settings.SHERRY_CONFIG_PATH", sherry)
        clean_state.store[("__global__", "external_path_yolo")] = True
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        with pytest.raises(PathOutOfBoundsError, match="deny list"):
            resolve_external_path(str(secret_dir / "token.txt"), session_id="s1")

        assert mock_interrupt.calls == []

    def test_missing_sherry_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.sherry_settings.SHERRY_CONFIG_PATH", tmp_path / "nope.jsonc")
        assert _get_yolo_deny_paths() == list(HITL_DEFAULTS["yolo_deny_paths"])

    def test_is_yolo_denied_expands_tilde(self):
        assert _is_yolo_denied(Path.home() / ".ssh" / "id_rsa") is True
        assert _is_yolo_denied(Path.home() / "public" / "notes.txt") is False


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

    def test_interrupt_payload_declares_four_decisions(self, tmp_path, clean_state, mock_interrupt):
        target = _external(tmp_path, "app.conf")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolve_external_path(target, session_id="s1", action_desc="read file")

        payload = mock_interrupt.calls[0]
        assert payload["action_requests"][0]["name"] == "external_file_access"
        assert payload["action_requests"][0]["args"] == {"path": target}
        review = payload["review_configs"][0]
        assert review["action_name"] == "external_file_access"
        assert review["allowed_decisions"] == ["approve", "approve_dir", "yolo", "reject"]


# ── H. Directory-level allowlist + approve_dir ──────────────────────────


class TestDirectoryAllowlist:
    def test_approve_dir_adds_parent_and_children_skip_interrupt(
        self, tmp_path, clean_state, mock_interrupt
    ):
        from runtime import state_register_mem

        data_dir = tmp_path.resolve() / "data"
        target = str(data_dir / "report.csv")
        mock_interrupt(response={"decisions": [{"type": "approve_dir"}]})

        resolved = resolve_external_path(target, session_id="s1")

        assert resolved == Path(target)
        assert state_register_mem.get_state("s1", "external_path_allowlist") == [
            str(data_dir) + "/"
        ]

        sibling = str(data_dir / "other.csv")
        mock_interrupt(response={"decisions": [{"type": "reject"}]})
        assert resolve_external_path(sibling, session_id="s1") == Path(sibling)
        assert len(mock_interrupt.calls) == 1, "directory entry covers its children"

    def test_directory_entry_matches_children_not_sibling_directories(
        self, tmp_path, clean_state, mock_interrupt
    ):
        data_dir = tmp_path.resolve() / "data"
        database_dir = tmp_path.resolve() / "database"
        _add_to_allowlist(data_dir, "s1", mode="dir")

        inside = str(data_dir / "x.csv")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})
        assert resolve_external_path(inside, session_id="s1") == Path(inside)
        assert mock_interrupt.calls == []

        sibling = str(database_dir / "x.csv")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})
        assert resolve_external_path(sibling, session_id="s1") == Path(sibling)
        assert len(mock_interrupt.calls) == 1, "sibling prefix must not match data/"

    def test_child_inherits_parent_directory_entry(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        data_dir = tmp_path.resolve() / "data"
        _add_to_allowlist(data_dir, "parent_s1", mode="dir")
        state_register_mem.set_state("child_1", "requester_session_key", "parent_s1")
        target = str(data_dir / "nested" / "x.csv")
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        assert resolve_external_path(target, session_id="child_1") == Path(target)
        assert mock_interrupt.calls == []

    def test_approve_still_adds_exact_file_only(self, tmp_path, clean_state, mock_interrupt):
        from runtime import state_register_mem

        data_dir = tmp_path.resolve() / "data"
        target = str(data_dir / "report.csv")
        mock_interrupt(response={"decisions": [{"type": "approve"}]})

        resolve_external_path(target, session_id="s1")

        assert state_register_mem.get_state("s1", "external_path_allowlist") == [target]
        sibling = str(data_dir / "other.csv")
        mock_interrupt(response={"decisions": [{"type": "reject"}]})

        with pytest.raises(PathOutOfBoundsError, match="denied"):
            resolve_external_path(sibling, session_id="s1")

    def test_add_dir_entry_is_idempotent(self, clean_state):
        from runtime import state_register_mem

        data_dir = Path("/home/u/data")
        _add_to_allowlist(data_dir, "s1", mode="dir")
        _add_to_allowlist(data_dir, "s1", mode="dir")

        assert state_register_mem.get_state("s1", "external_path_allowlist") == ["/home/u/data/"]


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
