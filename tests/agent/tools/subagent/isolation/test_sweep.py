"""Stale isolated-workspace sweep: orphaned / expired-conflict / corrupted."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent.tools.subagent.isolation import sweep as sweep_mod
from agent.tools.subagent.isolation.sweep import sweep_stale_isolated_workspaces

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def iso_root(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "isolated"
    base.mkdir()
    monkeypatch.setattr("config.path.isolated_workspaces_dir", lambda: base, raising=False)
    monkeypatch.setattr(sweep_mod, "isolated_workspaces_dir", lambda: base, raising=False)
    return base


def _workspace(
    base: Path,
    slug: str,
    *,
    manifest: str | None,
    parent_root: str = "/tmp/parent",
    age_days: float = 0,
) -> Path:
    ws = base / slug
    (ws / "tree").mkdir(parents=True)
    (ws / "tree" / "a.txt").write_text("x", encoding="utf-8")
    if manifest:
        (ws / manifest).write_text(
            json.dumps({"parent_root": parent_root, "child_session_key": slug}), encoding="utf-8"
        )
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(ws / manifest, (old, old))
    return ws


def _no_active_runs(monkeypatch) -> None:
    from agent.tools.subagent.registry import memory

    monkeypatch.setattr(memory, "values", lambda: [])


def test_orphaned_workspace_is_cleaned(iso_root, monkeypatch):
    _no_active_runs(monkeypatch)
    ws = _workspace(iso_root, "iso-aaaa", manifest="snapshot.json")
    discarded: list[Path] = []
    monkeypatch.setattr(
        sweep_mod, "discard_isolated_workspace", lambda path: discarded.append(path)
    )

    cleaned = sweep_stale_isolated_workspaces()

    assert cleaned == 1
    assert discarded == [ws]


def test_active_run_workspace_is_left_alone(iso_root, monkeypatch):
    from agent.tools.subagent.registry import memory
    from agent.tools.subagent.types.registry import SubagentRunRecord

    _workspace(iso_root, "iso-bbbb", manifest="snapshot.json")
    run = SubagentRunRecord(
        run_id="run-active",
        requester_session_key="agent:main:session:p",
        task="t",
        child_session_key="agent:main:subagent:x",
    )
    # The slug of that key is what the sweep compares against.
    from agent.tools.subagent.isolation.tree import _slug

    (iso_root / "iso-bbbb").rename(iso_root / _slug(run.child_session_key))
    monkeypatch.setattr(memory, "values", lambda: [run])
    discarded: list[Path] = []
    monkeypatch.setattr(
        sweep_mod, "discard_isolated_workspace", lambda path: discarded.append(path)
    )

    cleaned = sweep_stale_isolated_workspaces()

    assert cleaned == 0
    assert discarded == []


def test_conflict_workspace_within_ttl_is_kept(iso_root, monkeypatch):
    _no_active_runs(monkeypatch)
    _workspace(iso_root, "iso-cccc", manifest="merged.json", age_days=3)
    monkeypatch.setattr(sweep_mod, "discard_isolated_workspace", lambda path: None)

    assert sweep_stale_isolated_workspaces() == 0


def test_expired_conflict_workspace_is_cleaned(iso_root, monkeypatch):
    _no_active_runs(monkeypatch)
    ws = _workspace(iso_root, "iso-dddd", manifest="merged.json", age_days=8)
    discarded: list[Path] = []
    monkeypatch.setattr(
        sweep_mod, "discard_isolated_workspace", lambda path: discarded.append(path)
    )

    assert sweep_stale_isolated_workspaces() == 1
    assert discarded == [ws]


def test_corrupted_workspace_is_cleaned(iso_root, monkeypatch):
    _no_active_runs(monkeypatch)
    _workspace(iso_root, "iso-eeee", manifest=None)
    discarded: list[Path] = []
    monkeypatch.setattr(
        sweep_mod, "discard_isolated_workspace", lambda path: discarded.append(path)
    )

    assert sweep_stale_isolated_workspaces() == 1
    assert len(discarded) == 1


def test_unreadable_registry_cleans_nothing(iso_root, monkeypatch):
    """A registry read failure must never turn into a deletion."""
    from agent.tools.subagent.registry import memory

    def _boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(memory, "values", _boom)
    _workspace(iso_root, "iso-ffff", manifest="snapshot.json")
    monkeypatch.setattr(
        sweep_mod, "discard_isolated_workspace", lambda path: pytest.fail("deleted a workspace")
    )

    assert sweep_stale_isolated_workspaces() == 0


def test_parent_repo_gets_a_worktree_prune(iso_root, monkeypatch, tmp_path):
    _no_active_runs(monkeypatch)
    parent = tmp_path / "parent"
    parent.mkdir()
    _workspace(iso_root, "iso-gggg", manifest="snapshot.json", parent_root=str(parent))
    pruned: list[Path] = []
    monkeypatch.setattr(sweep_mod, "discard_isolated_workspace", lambda path: None)
    monkeypatch.setattr(sweep_mod, "_prune_worktrees", lambda root: pruned.append(root))

    sweep_stale_isolated_workspaces()

    assert pruned == [parent]


def test_missing_base_dir_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep_mod, "isolated_workspaces_dir", lambda: tmp_path / "nope")
    assert sweep_stale_isolated_workspaces() == 0


def test_sweep_never_raises(iso_root, monkeypatch):
    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(sweep_mod, "_collect_active_slugs", _boom)

    assert sweep_stale_isolated_workspaces() == 0


def test_non_workspace_directories_are_ignored(iso_root, monkeypatch):
    _no_active_runs(monkeypatch)
    (iso_root / "not-a-workspace").mkdir()
    monkeypatch.setattr(
        sweep_mod, "discard_isolated_workspace", lambda path: pytest.fail("touched a foreign dir")
    )

    assert sweep_stale_isolated_workspaces() == 0
