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


class TestTurnFloors:
    """A turn's floor is the tip that preceded its first snapshot.

    It is both the rollback boundary for editing that turn's message and the
    prune's protection: the cap is a soft cap on HISTORY, and a single turn may
    legitimately own more than ``max_snapshots`` commits (a fan-out child adds
    one each, an edit adds the pre/post-restore pair).
    """

    @pytest.fixture(autouse=True)
    def _isolated_floor_state(self, monkeypatch):
        # The module keeps process-level state; every test starts from a clean
        # slate so session ids may be reused.
        monkeypatch.setattr(shadow_git, "_dedup", set())
        monkeypatch.setattr(shadow_git, "_turn_counts", {})
        monkeypatch.setattr(shadow_git, "_floors", {})
        monkeypatch.setattr(shadow_git, "_floors_loaded", set())

    def test_the_first_snapshot_of_a_turn_records_the_previous_tip(self, project):
        base = shadow_git.take_snapshot(project, "manual:base")
        assert base
        shadow_git.new_turn("s")
        assert shadow_git.floor_for("s", project_root=project) is None

        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
        assert shadow_git.floor_for("s", project_root=project) == base
        state = project / ".sherry" / "shadow-git" / "state.json"
        assert state.is_file()

        # A later snapshot in the SAME turn must not move the boundary.
        (project / "a.txt").write_text("two\n", encoding="utf-8")
        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
        assert shadow_git.floor_for("s", project_root=project) == base

    def test_a_turn_without_snapshots_has_no_floor(self, project):
        shadow_git.take_snapshot(project, "manual:base")
        shadow_git.new_turn("s")
        assert shadow_git.floor_for("s", project_root=project) is None

    def test_a_new_turn_invalidates_the_old_floor_until_its_first_snapshot(self, project):
        shadow_git.take_snapshot(project, "manual:base")
        shadow_git.new_turn("s")
        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
        old_floor = shadow_git.floor_for("s", project_root=project)
        assert old_floor is not None

        shadow_git.new_turn("s")
        assert shadow_git.floor_for("s", project_root=project) is None
        previous_tip = shadow_git.run_shadow(
            project, ["rev-parse", shadow_git.SHADOW_REF]
        ).stdout.strip()
        tip = shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
        # The new turn's floor is the tip that preceded ITS first snapshot.
        assert shadow_git.floor_for("s", project_root=project) == previous_tip
        assert shadow_git.floor_for("s", project_root=project) != tip
        assert shadow_git.floor_for("s", project_root=project) != old_floor

    def test_pruning_never_crosses_an_active_turn_floor(self, project, monkeypatch):
        from config.features import SHADOW_GIT

        monkeypatch.setitem(SHADOW_GIT, "max_snapshots", 3)
        pre = shadow_git.take_snapshot(project, "manual:pre")
        base = shadow_git.take_snapshot(project, "manual:base")
        shadow_git.new_turn("s")
        for i in range(6):
            shadow_git.take_snapshot(project, f"manual:turn1-{i}", session_id="s")

        # The cap alone would leave 3; the active floor keeps the whole turn
        # (base + 6) and drops only the commit before the floor.
        assert _snapshot_count(project) == 7
        kept = shadow_git.run_shadow(project, ["rev-list", shadow_git.SHADOW_REF]).stdout.split()
        assert pre not in kept
        assert base in kept
        assert shadow_git.floor_for("s", project_root=project) == base

    def test_pruning_releases_the_overshoot_when_the_turn_ends(self, project, monkeypatch):
        from config.features import SHADOW_GIT

        monkeypatch.setitem(SHADOW_GIT, "max_snapshots", 3)
        shadow_git.take_snapshot(project, "manual:pre")
        shadow_git.take_snapshot(project, "manual:base")
        shadow_git.new_turn("s")
        for i in range(5):
            shadow_git.take_snapshot(project, f"manual:turn1-{i}", session_id="s")
        # The floor keeps the turn (6 commits); the commit BEFORE it was
        # already dropped by the first prune.
        assert _snapshot_count(project) == 6

        shadow_git.new_turn("s")  # the previous floor stops protecting
        shadow_git.take_snapshot(project, "manual:turn2", session_id="s")
        assert _snapshot_count(project) == 3


class TestRestoreToTurn:
    """Edit-message rewind: files first, then the caller truncates the chat."""

    @pytest.fixture(autouse=True)
    def _isolated_floor_state(self, monkeypatch):
        monkeypatch.setattr(shadow_git, "_dedup", set())
        monkeypatch.setattr(shadow_git, "_turn_counts", {})
        monkeypatch.setattr(shadow_git, "_floors", {})
        monkeypatch.setattr(shadow_git, "_floors_loaded", set())

    def test_reverts_the_turn_and_deletes_files_created_in_it(self, project):
        assert shadow_git.take_snapshot(project, "manual:base")
        shadow_git.new_turn("s")
        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")

        (project / "a.txt").write_text("two\n", encoding="utf-8")
        (project / "new.txt").write_text("new\n", encoding="utf-8")
        (project / "sub").mkdir()
        (project / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")

        result = shadow_git.restore_to_turn("s", 1, project_root=project)

        assert result["restored"] is True
        assert sorted(result["removed"]) == ["new.txt", "sub/nested.txt"]
        assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"
        assert not (project / "new.txt").exists()
        assert not (project / "sub").exists()

        # The chain tip now describes the RESTORED tree, so the re-run turn's
        # own floor is the rolled-back state (editing the re-run cannot
        # resurrect the files this rollback removed).
        shadow_git.new_turn("s")
        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
        followup = shadow_git.floor_for("s", project_root=project)
        assert followup
        tree = shadow_git.run_shadow(
            project, ["ls-tree", "-r", "--name-only", followup]
        ).stdout.split()
        assert tree == ["a.txt"]

    def test_without_a_floor_nothing_is_restored(self, project):
        shadow_git.take_snapshot(project, "manual:base")

        result = shadow_git.restore_to_turn("never-snapshotted", 1, project_root=project)

        assert result == {"restored": False, "reason": "no_checkpoint"}
        assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"

    def test_without_a_project_root_it_is_skipped(self):
        assert shadow_git.restore_to_turn("s", 1) == {"restored": False, "reason": "no_root"}

    def test_a_floor_from_a_previous_process_is_discarded(self, project, monkeypatch):
        shadow_git.take_snapshot(project, "manual:base")
        shadow_git.new_turn("s")
        shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
        assert shadow_git.floor_for("s", project_root=project) is not None

        # A restart: new boot id, empty in-memory counters. Turn numbers are
        # process-relative, so a collision (both processes at turn 1) must NOT
        # hand back the old boundary — that would roll back an older turn.
        monkeypatch.setattr(shadow_git, "_BOOT_ID", "another-boot")
        monkeypatch.setattr(shadow_git, "_floors", {})
        monkeypatch.setattr(shadow_git, "_floors_loaded", set())
        monkeypatch.setattr(shadow_git, "_turn_counts", {})

        assert shadow_git.floor_for("s", project_root=project) is None
        assert shadow_git.restore_to_turn("s", 1, project_root=project) == {
            "restored": False,
            "reason": "no_checkpoint",
        }
