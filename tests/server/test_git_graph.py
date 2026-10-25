"""The git-graph service now serves the project's shadow checkpoint chain.

The user's own ``.git`` is never read or written: the fixture creates shadow
snapshots with the same helper every write uses, then asserts the page, paging,
commit detail, restore action and the branchless checkout refusal.
"""

from __future__ import annotations

import pytest

from agent.tools.pub_base import shadow_git
from server.service import git_graph_service as service

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    shadow_git.take_snapshot(root, "before_write:write_file:a.txt", session_id="sess")
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    shadow_git.take_snapshot(root, "before_write:patch_file:a.txt", session_id="sess")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    shadow_git.take_snapshot(root, "before_write:write_file:b.txt", session_id="sess")
    (root / "dirty.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(service, "current_project_dir", lambda _sid: root)
    monkeypatch.setattr(service, "project_dir_source", lambda _sid: "session")
    return root


def test_the_graph_serves_the_shadow_chain(project):
    page = service.read_graph("sess", limit=10)

    assert page.available is True
    assert page.branch == "checkpoint"
    assert page.branches == []
    assert page.dirty == 1  # the external dirty.txt is not in the shadow index
    subjects = [commit["subject"] for commit in page.commits]
    assert subjects == [
        "before write_file b.txt",
        "before patch_file a.txt",
        "before write_file a.txt",
    ]
    tip = page.commits[0]
    assert tip["parents"], "a snapshot chain carries its parent"
    assert tip["short"] == tip["hash"][:8]
    assert page.has_more is False


def test_paging_reports_more_and_does_not_overlap(project):
    first = service.read_graph("sess", limit=2)
    second = service.read_graph("sess", limit=2, skip=2)

    assert len(first.commits) == 2 and first.has_more is True
    assert [c["subject"] for c in second.commits] == ["before write_file a.txt"]
    assert second.has_more is False


def test_commit_files_and_diff_come_from_the_shadow_tree(project):
    page = service.read_graph("sess", limit=10)
    target = next(c for c in page.commits if c["subject"] == "before patch_file a.txt")

    detail = service.read_commit_files("sess", target["hash"])
    assert [(f.status, f.path) for f in detail.files] == [("M", "a.txt")]
    diff = service.read_commit_diff("sess", target["hash"], "a.txt")
    texts = " ".join(
        (row.left or {}).get("text", "") + " " + (row.right or {}).get("text", "")
        for row in diff.rows
    )
    assert "one" in texts and "two" in texts


def test_reset_restores_the_checkpoint_without_touching_user_git(project):
    page = service.read_graph("sess", limit=10)
    oldest = page.commits[-1]["hash"]

    refreshed = service.reset_to("sess", oldest, "hard")

    assert (project / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert (project / ".git").exists() is False, "shadow restore must not create .git"
    assert refreshed.commits[0]["subject"] == "pre-restore (undo point)"


def test_checkout_is_refused_because_there_are_no_branches(project):
    with pytest.raises(service.GitActionError) as exc:
        service.checkout_ref("sess", "main")
    assert "branches" in str(exc.value)


def test_an_empty_project_is_available_with_an_empty_chain(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(service, "current_project_dir", lambda _sid: root)
    monkeypatch.setattr(service, "project_dir_source", lambda _sid: "session")

    page = service.read_graph("sess", limit=10)

    assert page.available is True
    assert page.commits == []
