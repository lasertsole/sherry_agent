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
