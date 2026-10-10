"""Boot reconciliation of session folders against the store records."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from server.service import session_dir_sweep as sweep_mod

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def sessions_root(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "sessions"
    base.mkdir()
    monkeypatch.setattr(sweep_mod, "SESSIONS_DIR", str(base))
    monkeypatch.setitem(sweep_mod.SESSION_DIRS, "boot_sweep_enabled", True)
    monkeypatch.setitem(sweep_mod.SESSION_DIRS, "min_dir_age_seconds", 0)
    return base


def _session_dir(base: Path, session_id: str, *, age_seconds: float = 3600) -> Path:
    folder = base / session_id
    (folder / "evicted").mkdir(parents=True)
    (folder / "evicted" / "ev-1.txt").write_text("evicted payload", encoding="utf-8")
    (folder / "media").mkdir()
    (folder / "media" / "clip.bin").write_bytes(b"x")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(folder, (old, old))
    return folder


def _no_records(monkeypatch) -> None:
    for probe in (
        "_messages_has_session",
        "_todos_has_session",
        "_taskflows_has_session",
        "_checkpoints_has_session",
    ):
        monkeypatch.setattr(sweep_mod, probe, lambda _sid: False)


def test_orphaned_session_dir_is_removed(sessions_root, monkeypatch):
    _no_records(monkeypatch)
    folder = _session_dir(sessions_root, "sess-orphan")

    cleaned = sweep_mod.sweep_orphaned_session_dirs()

    assert cleaned == 1
    assert not folder.exists()


def test_a_session_known_to_any_store_is_kept(sessions_root, monkeypatch):
    _no_records(monkeypatch)
    folder = _session_dir(sessions_root, "sess-live")
    # Only the todos store knows it: the scan must consult every store.
    monkeypatch.setattr(sweep_mod, "_todos_has_session", lambda _sid: True)

    assert sweep_mod.sweep_orphaned_session_dirs() == 0
    assert folder.exists()


def test_a_fresh_directory_is_never_scanned(sessions_root, monkeypatch):
    _no_records(monkeypatch)
    monkeypatch.setitem(sweep_mod.SESSION_DIRS, "min_dir_age_seconds", 300)
    folder = _session_dir(sessions_root, "sess-fresh", age_seconds=5)

    assert sweep_mod.sweep_orphaned_session_dirs() == 0
    assert folder.exists()


def test_an_unreadable_store_keeps_the_directory(sessions_root, monkeypatch):
    _no_records(monkeypatch)

    def _boom(_sid):
        raise RuntimeError("db locked")

    monkeypatch.setattr(sweep_mod, "_messages_has_session", _boom)
    folder = _session_dir(sessions_root, "sess-unknown")

    # The real probe catches its own errors and answers "keep".
    assert sweep_mod.sweep_orphaned_session_dirs() == 0
    assert folder.exists()


def test_symlinks_are_skipped(sessions_root, monkeypatch, tmp_path):
    _no_records(monkeypatch)
    # The target lives OUTSIDE the sessions root: inside it, the scan would
    # (rightly) treat the target itself as an orphaned session directory.
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("must survive", encoding="utf-8")
    link = sessions_root / "sess-link"
    link.symlink_to(target)

    assert sweep_mod.sweep_orphaned_session_dirs() == 0
    assert target.exists() and (target / "keep.txt").exists()


def test_unsafe_directory_names_are_skipped(sessions_root, monkeypatch):
    """A name the validator rejects is never purged (the guard is the boundary)."""
    _no_records(monkeypatch)
    monkeypatch.setattr(sweep_mod, "is_safe_session_id", lambda _name: False)
    folder = _session_dir(sessions_root, "sess-unsafe")

    assert sweep_mod.sweep_orphaned_session_dirs() == 0
    assert folder.exists()


def test_the_switch_disables_the_scan(sessions_root, monkeypatch):
    _no_records(monkeypatch)
    monkeypatch.setitem(sweep_mod.SESSION_DIRS, "boot_sweep_enabled", False)
    folder = _session_dir(sessions_root, "sess-off")

    assert sweep_mod.sweep_orphaned_session_dirs() == 0
    assert folder.exists()


def test_a_missing_root_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep_mod, "SESSIONS_DIR", str(tmp_path / "nope"))

    assert sweep_mod.sweep_orphaned_session_dirs() == 0


def test_the_sweep_never_raises(sessions_root, monkeypatch):
    def _boom(_sid):
        raise OSError("disk gone")

    monkeypatch.setattr(sweep_mod, "_session_has_any_records", _boom)

    assert sweep_mod.sweep_orphaned_session_dirs() == 0


def test_evicted_and_media_counts_are_logged(sessions_root, monkeypatch, caplog):
    _no_records(monkeypatch)
    _session_dir(sessions_root, "sess-counts")
    records: list[str] = []
    monkeypatch.setattr(
        sweep_mod.logger, "info", lambda message, *args, **kw: records.append(message.format(*args))
    )

    sweep_mod.sweep_orphaned_session_dirs()

    assert any("1 evicted" in line and "1 media" in line for line in records), records
