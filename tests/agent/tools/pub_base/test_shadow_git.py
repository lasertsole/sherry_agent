"""Shadow git: per-project snapshots, never the user's .git."""

from __future__ import annotations

import pytest

from agent.tools.pub_base import shadow_git

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    return root


def test_snapshot_creates_the_store_not_a_user_repository(project):
    sha = shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
    assert sha
    assert (project / ".sherry" / "shadow-git" / "HEAD").is_file()
    assert not (project / ".git").exists()
    tree = shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.SHADOW_REF]
    ).stdout.split()
    assert tree == ["a.txt"]


def test_secrets_and_the_store_are_excluded(project):
    (project / ".env").write_text("KEY=secret\n", encoding="utf-8")
    (project / ".env.example").write_text("KEY=\n", encoding="utf-8")
    (project / ".models").write_text("secret\n", encoding="utf-8")
    shadow_git.take_snapshot(project, "manual:first")
    tree = shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.SHADOW_REF]
    ).stdout.split()
    assert "a.txt" in tree
    assert ".env.example" in tree
    assert ".env" not in tree
    assert ".models" not in tree
    assert not any(path.startswith(".sherry/") for path in tree)


def test_list_checkpoints_parses_the_structured_message(project):
    shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
    items = shadow_git.list_checkpoints("s", limit=10)
    # list_checkpoints resolves the session root through the state register;
    # the helper call above does not bind one, so read via run_shadow instead.
    assert items == []
    raw = shadow_git.run_shadow(
        project, ["log", "-1", "--pretty=%s", shadow_git.SHADOW_REF]
    ).stdout.strip()
    assert "type=before_write|tool=write_file|target=a.txt" in raw


def test_restore_all_and_one_file(project, monkeypatch):
    import agent.tools.pub_base.path_utils as path_utils

    monkeypatch.setattr(path_utils, "session_workspace_root", lambda _sid: project)
    first = shadow_git.take_snapshot(project, "manual:one")
    (project / "a.txt").write_text("two\n", encoding="utf-8")
    (project / "b.txt").write_text("b\n", encoding="utf-8")
    shadow_git.take_snapshot(project, "manual:two")

    shadow_git.restore_checkpoint("s", first, file_path="a.txt")
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (project / "b.txt").read_text(encoding="utf-8") == "b\n"

    shadow_git.restore_checkpoint("s", first)
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_a_broken_store_is_fail_open(project, monkeypatch):
    import agent.tools.pub_base.path_utils as path_utils

    monkeypatch.setattr(path_utils, "session_workspace_root", lambda _sid: project)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(shadow_git, "take_snapshot", _boom)
    shadow_git.new_turn("s")
    assert shadow_git.ensure_checkpoint("s", reason="before_write:write_file:a.txt") is False


def test_store_dir_override_moves_the_store_out_of_the_project(project, tmp_path, monkeypatch):
    from config.features import SHADOW_GIT

    store_root = tmp_path / "shadow-store"
    monkeypatch.setitem(SHADOW_GIT, "store_dir", str(store_root))

    sha = shadow_git.take_snapshot(project, "manual:external", session_id="s")

    assert sha
    assert not (project / ".sherry").exists()
    store = shadow_git.shadow_store(project)
    assert store.is_relative_to(store_root)
    assert (store / "HEAD").is_file()
    tree = shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.SHADOW_REF]
    ).stdout.split()
    assert tree == ["a.txt"]


def test_freeze_blocks_new_snapshots_and_wait_for_idle_tracks_writers(project, monkeypatch):
    import threading

    shadow_git.freeze(project)
    try:
        assert shadow_git.take_snapshot(project, "manual:frozen") is None
        assert not (project / ".sherry" / "shadow-git" / "HEAD").exists()
        assert (
            shadow_git.ensure_checkpoint(
                "s", reason="before_write:write_file:a.txt", project_root=project
            )
            is True
        )
    finally:
        shadow_git.unfreeze(project)

    started = threading.Event()
    release = threading.Event()

    def _slow(*_args, **_kwargs):
        started.set()
        release.wait(5)
        return "sha"

    monkeypatch.setattr(shadow_git, "_take_snapshot_locked", _slow)
    thread = threading.Thread(
        target=shadow_git.take_snapshot, args=(project, "manual:slow"), daemon=True
    )
    thread.start()
    assert started.wait(5)
    assert shadow_git.active_writers(project) == 1
    assert shadow_git.wait_for_idle(project, timeout=0.05) is False
    release.set()
    thread.join(5)
    assert shadow_git.wait_for_idle(project, timeout=1) is True
    assert shadow_git.active_writers(project) == 0


def _snapshot_count(project) -> int:
    """The store's snapshot-chain length."""
    out = shadow_git.run_shadow(
        project, ["rev-list", "--count", shadow_git.SHADOW_REF]
    ).stdout.strip()
    return int(out)


class TestSnapshotCap:
    """The cap is PER PROJECT ROOT and shared by every session (subagents too).

    A 20-snapshot budget was consumed in one or two rounds of a concurrent
    fan-out, which collapsed the restore window to the last step or two.
    """

    def test_the_shipped_cap_is_one_hundred(self):
        from config.features import SHADOW_GIT

        assert SHADOW_GIT["max_snapshots"] == 100, (
            "raise/lower this deliberately: the store is shared per project root"
        )

    def test_under_the_cap_nothing_is_pruned(self, project, monkeypatch):
        from config.features import SHADOW_GIT

        monkeypatch.setitem(SHADOW_GIT, "max_snapshots", 3)
        for i in range(2):
            shadow_git.take_snapshot(project, f"manual:s{i}")

        assert _snapshot_count(project) == 2

    def test_pruning_keeps_exactly_the_cap(self, project, monkeypatch):
        from config.features import SHADOW_GIT

        monkeypatch.setitem(SHADOW_GIT, "max_snapshots", 3)
        for i in range(6):
            shadow_git.take_snapshot(project, f"manual:s{i}")

        assert _snapshot_count(project) == 3

    @pytest.mark.timeout(180)
    def test_a_hundred_and_one_snapshots_prune_to_the_shipped_cap(self, project):
        """The plan's acceptance case, against the real default (no override)."""
        for i in range(101):
            shadow_git.take_snapshot(project, f"manual:s{i}")

        assert _snapshot_count(project) == 100
