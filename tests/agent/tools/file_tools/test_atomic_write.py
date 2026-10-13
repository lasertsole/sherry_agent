"""Atomic-write helper: crash-safety, symlink policy, revision precondition.

The helper backs every file-tool write, so the properties pinned here are the
ones the tools' contract leans on: a reader sees old-or-new content (never a
torn file), a script's mode survives the inode swap, a symlinked target is
REFUSED (never followed — unlike ``pub/func/atomic_replace.py``), an optional
``expected_revision`` refuses a write whose target moved underneath it, and a
kill -9 leftover (``.sherry-tmp-*.swp``) is swept by the next write into the
directory.
"""

import json
import os
import stat
import time

import pytest

from agent.tools.pub_base import atomic_write as atomic_write_mod
from agent.tools.pub_base.atomic_write import (
    StaleWriteError,
    atomic_write_text_no_follow,
    file_revision,
    revision_id,
    sweep_stale_temp_files,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def target(tmp_path):
    return tmp_path / "file.txt"


def test_creates_the_file_with_default_mode(target):
    atomic_write_text_no_follow(target, "hello")

    assert target.read_text(encoding="utf-8") == "hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_replaces_content_and_keeps_the_original_mode(target):
    target.write_text("old", encoding="utf-8")
    target.chmod(0o755)

    atomic_write_text_no_follow(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    # A script's execute bit must survive the inode swap (ZCode's atomicWrite
    # chmods for exactly this reason).
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_refuses_a_symlinked_target(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(victim, link)
    except OSError:
        pytest.skip("cannot create symlink on this platform")

    with pytest.raises(OSError) as excinfo:
        atomic_write_text_no_follow(link, "through the link")

    assert "Symbolic link" in str(excinfo.value)
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "untouched"


def test_matching_revision_writes(target):
    target.write_text("old", encoding="utf-8")
    revision = file_revision(target)

    atomic_write_text_no_follow(target, "new", expected_revision=revision)

    assert target.read_text(encoding="utf-8") == "new"


def test_stale_revision_is_refused_and_leaves_the_file_alone(target):
    target.write_text("old", encoding="utf-8")

    with pytest.raises(StaleWriteError):
        atomic_write_text_no_follow(target, "new", expected_revision="mtime:0:size:0")

    assert target.read_text(encoding="utf-8") == "old"
    # The temporary file never survives a refused write.
    assert not list(target.parent.glob(".sherry-tmp-*"))


def test_revision_changed_between_the_temp_write_and_the_replace(target, monkeypatch):
    """The second CAS layer: the file moves AFTER the temp file exists."""
    target.write_text("old", encoding="utf-8")
    revision = file_revision(target)
    real_revision = atomic_write_mod.file_revision

    def racing(path):
        # Simulate another writer landing between the caller's probe and the
        # replace: mutate the target first, then answer truthfully.
        target.write_text("someone else wrote", encoding="utf-8")
        return real_revision(path)

    monkeypatch.setattr(atomic_write_mod, "file_revision", racing)

    with pytest.raises(StaleWriteError):
        atomic_write_text_no_follow(target, "mine", expected_revision=revision)

    assert target.read_text(encoding="utf-8") == "someone else wrote"
    assert not list(target.parent.glob(".sherry-tmp-*"))


def test_rename_failure_falls_back_to_an_in_place_write(target, monkeypatch):
    from loguru import logger

    target.write_text("old", encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("rename unsupported here")

    monkeypatch.setattr(atomic_write_mod, "_replace", boom)

    # loguru is not stdlib logging, so caplog never sees these records.
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message), level="WARNING")
    try:
        atomic_write_text_no_follow(target, "new")
    finally:
        logger.remove(sink_id)

    assert target.read_text(encoding="utf-8") == "new"
    assert not list(target.parent.glob(".sherry-tmp-*"))
    # The degradation is visible in the log, not silent.
    assert any("in-place write" in message for message in captured), captured


def test_revision_id_shape(target):
    target.write_text("abc", encoding="utf-8")
    revision = file_revision(target)

    assert revision.startswith("mtime:")
    assert ":size:3" in revision
    assert file_revision(target.parent / "missing.txt") == "absent"
    assert revision_id(None) == "absent"


def test_write_result_is_valid_json_ready(target):
    """Sanity: the helper does not add or strip anything the tools rely on."""
    payload = {"k": "值"}
    atomic_write_text_no_follow(target, json.dumps(payload, ensure_ascii=False))

    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_sweep_removes_only_old_temp_files(tmp_path):
    old = tmp_path / ".sherry-tmp-abc.swp"
    old.write_text("abandoned by a kill -9", encoding="utf-8")
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(old, (two_hours_ago, two_hours_ago))
    fresh = tmp_path / ".sherry-tmp-live.swp"
    fresh.write_text("a write is in flight", encoding="utf-8")
    unrelated = tmp_path / "other.swp"
    unrelated.write_text("not ours", encoding="utf-8")
    wrong_suffix = tmp_path / ".sherry-tmp-x.txt"
    wrong_suffix.write_text("not ours", encoding="utf-8")

    removed = sweep_stale_temp_files(tmp_path)

    assert removed == 1
    assert not old.exists()
    # A live writer's temp file is seconds old: the age threshold keeps the
    # sweep away from it, and foreign names are never touched.
    assert fresh.exists()
    assert unrelated.exists()
    assert wrong_suffix.exists()


def test_sweep_leaves_symlinks_and_missing_directories_alone(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    link = tmp_path / ".sherry-tmp-link.swp"
    try:
        os.symlink(victim, link)
    except OSError:
        pytest.skip("cannot create symlink on this platform")
    long_ago = time.time() - 2 * 60 * 60
    os.utime(link, (long_ago, long_ago), follow_symlinks=False)

    assert sweep_stale_temp_files(tmp_path) == 0
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "untouched"
    # A scan that cannot even open the directory reports nothing instead of
    # failing the write it precedes.
    assert sweep_stale_temp_files(tmp_path / "missing") == 0


def test_a_write_sweeps_the_directory_it_lands_in(target, tmp_path):
    old = tmp_path / ".sherry-tmp-crashed.swp"
    old.write_text("left behind", encoding="utf-8")
    long_ago = time.time() - 2 * 60 * 60
    os.utime(old, (long_ago, long_ago))

    atomic_write_text_no_follow(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert not old.exists()
