"""Shadow git: per-project snapshots, never the user's .git."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base import shadow_git

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _isolated_shared_store(tmp_path, monkeypatch):
    """One shared store per test, plus a clean process-level floor slate."""
    from config.features import SHADOW_GIT

    monkeypatch.setitem(SHADOW_GIT, "store_dir", str(tmp_path / "shared-store"))
    monkeypatch.setattr(shadow_git, "_dedup", set())
    monkeypatch.setattr(shadow_git, "_turn_counts", {})
    monkeypatch.setattr(shadow_git, "_floors", {})
    monkeypatch.setattr(shadow_git, "_floors_loaded", set())
    monkeypatch.setattr(shadow_git, "_frozen", set())
    monkeypatch.setattr(shadow_git, "_active_writers", {})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    return root


def test_snapshot_creates_the_store_not_a_user_repository(project):
    sha = shadow_git.take_snapshot(project, "before_write:write_file:a.txt", session_id="s")
    assert sha
    assert (shadow_git.shadow_store(project) / "HEAD").is_file()
    assert shadow_git.shadow_meta(project).is_file()
    assert not (project / ".sherry" / "shadow-git").exists()
    assert not (project / ".git").exists()
    tree = shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.shadow_ref(project)]
    ).stdout.split()
    assert tree == ["a.txt"]


def test_secrets_and_the_store_are_excluded(project):
    (project / ".env").write_text("KEY=secret\n", encoding="utf-8")
    (project / ".env.example").write_text("KEY=\n", encoding="utf-8")
    (project / ".models").write_text("secret\n", encoding="utf-8")
    shadow_git.take_snapshot(project, "manual:first")
    tree = shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.shadow_ref(project)]
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
        project, ["log", "-1", "--pretty=%s", shadow_git.shadow_ref(project)]
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
    assert store == store_root.resolve()
    assert (store / "HEAD").is_file()
    assert shadow_git.shadow_meta(project).is_file()
    tree = shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.shadow_ref(project)]
    ).stdout.split()
    assert tree == ["a.txt"]


def test_freeze_blocks_new_snapshots_and_wait_for_idle_tracks_writers(project, monkeypatch):
    import threading

    shadow_git.freeze(project)
    try:
        assert shadow_git.take_snapshot(project, "manual:frozen") is None
        assert not (shadow_git.shadow_store(project) / "HEAD").exists()
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
        project, ["rev-list", "--count", shadow_git.shadow_ref(project)]
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
        state = shadow_git.shadow_meta(project)
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
            project, ["rev-parse", shadow_git.shadow_ref(project)]
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
        kept = shadow_git.run_shadow(
            project, ["rev-list", shadow_git.shadow_ref(project)]
        ).stdout.split()
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


def _write_legacy_store(project) -> str:
    """Create a legacy ``.sherry/shadow-git`` bare repo with one commit."""
    import json
    import os
    import subprocess

    legacy = project / ".sherry" / "shadow-git"
    legacy.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(legacy)], check=True)
    env = {
        **os.environ,
        "GIT_DIR": str(legacy),
        "GIT_WORK_TREE": str(project),
        "GIT_INDEX_FILE": str(project.parent / "legacy-index"),
        "GIT_AUTHOR_NAME": "legacy",
        "GIT_AUTHOR_EMAIL": "legacy@localhost",
        "GIT_COMMITTER_NAME": "legacy",
        "GIT_COMMITTER_EMAIL": "legacy@localhost",
    }
    subprocess.run(["git", "add", "-A"], cwd=str(project), env=env, check=True)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=str(project), env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "commit-tree", tree, "-m", "legacy snapshot"],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit], env=env, check=True)
    (legacy / "state.json").write_text(
        json.dumps({"boot": "old-boot", "floors": {"old-session": {"turn": 1, "tip": commit}}}),
        encoding="utf-8",
    )
    return commit


def test_shared_store_isolates_refs_and_indexes_per_project(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.txt").write_text("b\n", encoding="utf-8")

    shadow_git.take_snapshot(project, "manual:a")
    assert shadow_git.shadow_head(project) is not None
    assert shadow_git.shadow_head(other) is None
    assert shadow_git.shadow_ref(other) not in shadow_git.run_shadow(project, ["show-ref"]).stdout
    assert shadow_git.shadow_index(other).exists() is False

    shadow_git.take_snapshot(other, "manual:b")
    assert shadow_git.shadow_head(other) is not None
    assert shadow_git.shadow_index(project).exists()
    assert shadow_git.shadow_index(other).exists()
    assert shadow_git.shadow_index(project) != shadow_git.shadow_index(other)
    assert shadow_git.run_shadow(
        project, ["ls-tree", "-r", "--name-only", shadow_git.shadow_ref(project)]
    ).stdout.split() == ["a.txt"]
    assert shadow_git.run_shadow(
        other, ["ls-tree", "-r", "--name-only", shadow_git.shadow_ref(other)]
    ).stdout.split() == ["b.txt"]


def test_projects_json_carries_workdir_and_floors(project):
    import json

    shadow_git.take_snapshot(project, "manual:base")
    shadow_git.new_turn("s")
    shadow_git.take_snapshot(project, "manual:turn", session_id="s")

    meta = json.loads(shadow_git.shadow_meta(project).read_text(encoding="utf-8"))
    assert meta["workdir"] == str(project.resolve())
    assert meta["created_at"]
    assert meta["last_touch"]
    assert meta["boot"]
    assert meta["floors"]["s"]["turn"] == 1
    assert meta["floors"]["s"]["tip"]


def test_migrates_a_legacy_per_project_store(project):
    import json

    legacy_tip = _write_legacy_store(project)
    assert (project / ".sherry" / "shadow-git" / "HEAD").is_file()

    # A read/write path initializes the shared store and runs the lazy import
    # WITHOUT taking a new snapshot, so the copied legacy floors are visible.
    shadow_git.run_shadow(project, ["show-ref"])
    meta = json.loads(shadow_git.shadow_meta(project).read_text(encoding="utf-8"))
    assert meta["workdir"] == str(project.resolve())
    assert meta["floors"]["old-session"]["tip"] == legacy_tip
    assert not (project / ".sherry" / "shadow-git").exists()
    assert (project / ".sherry" / "shadow-git.migrated" / "HEAD").is_file()
    assert shadow_git.shadow_head(project) is not None
    assert (
        shadow_git.run_shadow(project, ["cat-file", "-e", f"{legacy_tip}^{{commit}}"]).returncode
        == 0
    )

    created = meta["created_at"]
    shadow_git.take_snapshot(project, "manual:again")
    again = json.loads(shadow_git.shadow_meta(project).read_text(encoding="utf-8"))
    assert again["created_at"] == created


def test_migrate_legacy_can_be_disabled(project, monkeypatch):
    from config.features import SHADOW_GIT

    _write_legacy_store(project)
    monkeypatch.setitem(SHADOW_GIT, "migrate_legacy", False)

    shadow_git.take_snapshot(project, "manual:no-migration")

    assert (project / ".sherry" / "shadow-git" / "HEAD").is_file()
    assert shadow_git.shadow_head(project) is not None


def test_migrate_legacy_is_fail_open(project):
    """A legacy store without ``refs/heads/main`` must not block a snapshot."""
    legacy = project / ".sherry" / "shadow-git"
    legacy.mkdir(parents=True)
    (legacy / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    sha = shadow_git.take_snapshot(project, "manual:new-after-failed-migration")

    assert sha
    assert (project / ".sherry" / "shadow-git").is_dir()
    assert shadow_git.shadow_meta(project).is_file()
    assert shadow_git.shadow_head(project) is not None


def test_default_store_root_is_the_app_home_checkpoint_store(project, monkeypatch):
    from config.features import SHADOW_GIT
    from config.path import APP_HOME

    monkeypatch.setitem(SHADOW_GIT, "store_dir", "")
    monkeypatch.delenv("SHERRY_SHADOW_GIT_DIR", raising=False)

    assert shadow_git.shadow_store(project) == Path(APP_HOME) / "checkpoints" / "store"


def test_public_git_head_reads_the_override_store(project, tmp_path):
    from pub.git_head import shadow_head

    other = tmp_path / "other"
    other.mkdir()
    shadow_git.take_snapshot(project, "manual:x")

    assert shadow_head(project) is not None
    assert shadow_head(other) is None


class TestMementoFacade:
    """The additive Memento facade: a token, ``save()``, ``restore(token)``.

    The facade must never grow its own git logic — every operation delegates to
    the module-level primitives, so the chain/prune/floor semantics stay
    exactly what the tests above pin.
    """

    def test_save_returns_an_immutable_token(self, project):
        from dataclasses import FrozenInstanceError

        memento = shadow_git.WorkspaceMemento(project, session_id="s")

        token = memento.save("manual:memento")

        assert token is not None
        assert token.commit
        assert token.project_root == project.resolve()
        assert token.turn == 0
        assert token.reason == "manual:memento"
        assert token.session_id == "s"
        assert token.short_commit == token.commit[:7]
        assert token.to_dict()["commit"] == token.commit
        with pytest.raises(FrozenInstanceError):
            setattr(token, "commit", "another-commit")

    def test_save_returns_none_when_the_root_is_frozen(self, project):
        memento = shadow_git.WorkspaceMemento(project)

        shadow_git.freeze(project)
        try:
            assert memento.save("manual:frozen") is None
        finally:
            shadow_git.unfreeze(project)

    def test_restore_delegates_to_restore_checkpoint(self, project, monkeypatch):
        memento = shadow_git.WorkspaceMemento(project, session_id="s")
        token = memento.save("manual:base")
        assert token is not None

        calls: list[tuple[str, str, str | None, Path | None]] = []

        def _fake_restore(session_id, commit, *, file_path=None, project_root=None):
            calls.append((session_id, commit, file_path, project_root))
            return {"restored_to": commit, "file": file_path or "all"}

        monkeypatch.setattr(shadow_git, "restore_checkpoint", _fake_restore)

        result = memento.restore(token, file_path="a.txt")

        assert result == {"restored_to": token.commit, "file": "a.txt"}
        assert calls == [("s", token.commit, "a.txt", project.resolve())]

    def test_restore_reverts_the_saved_state(self, project):
        memento = shadow_git.WorkspaceMemento(project, session_id="s")
        token = memento.save("manual:base")
        assert token is not None
        (project / "a.txt").write_text("two\n", encoding="utf-8")

        memento.restore(token)

        assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"

        (project / "a.txt").write_text("three\n", encoding="utf-8")
        memento.restore(token, file_path="a.txt")
        assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"

    def test_restore_refuses_a_token_from_another_workspace(self, project, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        (other / "b.txt").write_text("b\n", encoding="utf-8")
        other_token = shadow_git.WorkspaceMemento(other).save("manual:other")
        assert other_token is not None

        with pytest.raises(ValueError, match="belongs to"):
            shadow_git.WorkspaceMemento(project).restore(other_token)

    def test_floor_and_restore_turn_round_trip(self, project):
        memento = shadow_git.WorkspaceMemento(project, session_id="s")
        base = memento.save("manual:base")
        assert base is not None
        shadow_git.new_turn("s")
        assert memento.floor() is None  # nothing snapshotted in this turn yet

        assert memento.save("before_write:write_file:a.txt") is not None
        floor = memento.floor()
        assert floor is not None
        assert floor.commit == base.commit
        assert floor.reason == "floor"
        assert floor.turn == 1

        (project / "a.txt").write_text("two\n", encoding="utf-8")
        (project / "new.txt").write_text("new\n", encoding="utf-8")

        result = memento.restore_turn(1)

        assert result["restored"] is True
        assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"
        assert not (project / "new.txt").exists()
