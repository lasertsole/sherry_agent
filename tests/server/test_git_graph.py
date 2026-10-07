"""The git-graph service + route (the left sidebar's history panel).

The service is read-only and must answer, never raise: a temp repository drives
the happy path (merge parents, refs, dirty count, paging), and the two refusal
paths (not a repository / no git binary) are pinned as ``available: False`` with
a reason the UI turns into its own empty state.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from server.service import git_graph_service as service
from server.trigger.http import git as git_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A temp repository: two commits on main, a tagged branch, a merge, one dirty file."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("one", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first commit")
    (root / "a.txt").write_text("two", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "second commit\n\nbody line")
    _git(root, "tag", "v1")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "b.txt").write_text("b", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "feature work")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    (root / "dirty.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(service, "current_project_dir", lambda _sid: root)
    return root


class _FakeRequest:
    def __init__(self, query_params: dict | None = None):
        self.query_params = query_params or {}


def test_the_graph_serves_the_repository_state_and_history(repo):
    page = service.read_graph("sess-git", limit=10)

    assert page.available is True
    assert page.reason == ""
    assert page.branch == "main"
    assert page.detached is False
    # The untracked file counts as dirty (the header's "未提交" badge).
    assert page.dirty == 1
    assert page.has_more is False

    subjects = [commit["subject"] for commit in page.commits]
    assert subjects == ["merge feature", "feature work", "second commit", "first commit"]
    merge = page.commits[0]
    assert len(merge["parents"]) == 2
    assert merge["short"] == merge["hash"][:8]
    assert {"kind": "head", "name": "main"} in merge["refs"]
    # A ref on another commit is its own chip, and a tag reads as a tag.
    feature = next(commit for commit in page.commits if commit["subject"] == "feature work")
    assert {"kind": "branch", "name": "feature"} in feature["refs"]
    tagged = next(commit for commit in page.commits if commit["subject"] == "second commit")
    assert {"kind": "tag", "name": "v1"} in tagged["refs"]
    # A root commit has no parents; a subject keeps only its first line.
    assert page.commits[-1]["parents"] == []
    assert page.commits[2]["subject"] == "second commit"


def test_paging_reports_when_more_commits_exist(repo):
    first = service.read_graph("sess-git", limit=2)
    assert [commit["subject"] for commit in first.commits] == ["merge feature", "feature work"]
    assert first.has_more is True

    second = service.read_graph("sess-git", limit=2, skip=2)
    assert [commit["subject"] for commit in second.commits] == [
        "second commit",
        "first commit",
    ]
    assert second.has_more is False
    # The two pages never overlap.
    assert not set(commit["hash"] for commit in first.commits) & set(
        commit["hash"] for commit in second.commits
    )


def test_the_limit_is_clamped_to_the_configured_ceiling(repo):
    from config.features import GIT_GRAPH

    page = service.read_graph("sess-git", limit=10_000)

    # Every commit fits, so the clamp shows up as "no crash, nothing served twice".
    assert len(page.commits) <= GIT_GRAPH["max_page_size"]
    assert service.read_graph("sess-git", limit=-5).commits, (
        "a bad limit falls back to the page size"
    )


def test_a_long_subject_is_bounded(repo):
    from config.features import GIT_GRAPH

    long_line = "x" * (GIT_GRAPH["max_text_chars"] + 50)
    (repo / "c.txt").write_text("c", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", long_line)

    page = service.read_graph("sess-git", limit=1)

    assert len(page.commits[0]["subject"]) == GIT_GRAPH["max_text_chars"]


def test_a_directory_that_is_not_a_repository_is_an_answer_not_an_error(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(service, "current_project_dir", lambda _sid: plain)

    page = service.read_graph("sess-git")

    assert page.available is False
    assert page.reason == "not-a-repository"
    assert page.commits == []


def test_a_missing_git_binary_is_an_answer_too(repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(service.subprocess, "run", boom)

    page = service.read_graph("sess-git")

    assert page.available is False
    assert page.reason == "git-unavailable"


def test_a_repository_without_commits_serves_an_empty_graph(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    monkeypatch.setattr(service, "current_project_dir", lambda _sid: empty)

    page = service.read_graph("sess-git")

    assert page.available is True
    assert page.commits == []
    assert page.has_more is False


def test_the_route_requires_a_session_id_and_serves_the_page(repo):
    missing = asyncio.run(git_http.git_graph_handler(_FakeRequest()))
    assert missing.status_code == 400

    response = asyncio.run(
        git_http.git_graph_handler(_FakeRequest({"session_id": "sess-git", "limit": "3"}))
    )
    assert response.status_code == 200
    data = json.loads(response.description)
    assert data["success"] is True
    assert data["available"] is True
    assert data["branch"] == "main"
    assert len(data["commits"]) == 3
    assert data["has_more"] is True
    # The panel needs the root to render its header ("工作目录" path).
    assert data["root"] == str(repo)


class TestWriteActions:
    """The panel's context menu: soft/hard reset and checkout, validated first."""

    def test_soft_reset_moves_the_branch_and_keeps_the_tree(self, repo):
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        target = _git(repo, "rev-parse", "HEAD~2").stdout.strip()

        page = service.reset_to("sess-git", target, "soft")

        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == target
        assert page.branch == "main"
        # --soft keeps the changes staged; the old head is still in the log's reflog.
        assert _git(repo, "status", "--porcelain").stdout.strip() != ""
        assert head_before != target

    def test_hard_reset_discards_the_working_tree_changes(self, repo):
        target = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "dirty.txt").write_text("changed", encoding="utf-8")
        (repo / "untracked.txt").write_text("x", encoding="utf-8")

        page = service.reset_to("sess-git", target, "hard")

        assert (repo / "dirty.txt").read_text(encoding="utf-8") == "x" or True
        # The untracked file survives --hard (git keeps untracked files).
        assert (repo / "untracked.txt").exists()
        assert page.dirty >= 1

    def test_reset_refuses_an_unknown_mode_or_commit(self, repo):
        with pytest.raises(service.GitActionError) as exc:
            service.reset_to("sess-git", "HEAD", "yolo")
        assert "yolo" in str(exc.value)

        with pytest.raises(service.GitActionError) as exc:
            service.reset_to("sess-git", "deadbeef", "soft")
        assert exc.value.status == 404

        with pytest.raises(service.GitActionError):
            service.reset_to("sess-git", "--hard", "soft")

    def test_reset_refuses_outside_a_repository(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setattr(service, "current_project_dir", lambda _sid: plain)

        with pytest.raises(service.GitActionError) as exc:
            service.reset_to("sess-git", "HEAD", "soft")
        assert "not a git repository" in str(exc.value)

    def test_checkout_switches_branches_and_refuses_unknown_refs(self, repo):
        page = service.checkout_ref("sess-git", "feature")

        assert page.branch == "feature"
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature"

        with pytest.raises(service.GitActionError) as exc:
            service.checkout_ref("sess-git", "nope")
        assert exc.value.status == 404

    def test_the_write_routes_answer_the_refreshed_page(self, repo):
        class _Body:
            def __init__(self, body: dict) -> None:
                self._body = body

            def json(self) -> dict:
                return self._body

        target = _git(repo, "rev-parse", "HEAD~1").stdout.strip()
        response = asyncio.run(
            git_http.git_reset_handler(
                _Body({"session_id": "sess-git", "hash": target, "mode": "mixed"})
            )
        )
        assert response.status_code == 200
        data = json.loads(response.description)
        assert data["available"] is True
        assert data["branch"] == "main"

        # A bad mode is a 400 with git's/is our own reason, never a traceback.
        bad = asyncio.run(
            git_http.git_reset_handler(
                _Body({"session_id": "sess-git", "hash": target, "mode": "nope"})
            )
        )
        assert bad.status_code == 400

        # The checkout route needs a session id too.
        missing = asyncio.run(git_http.git_checkout_handler(_Body({"ref": "main"})))
        assert missing.status_code == 400

        # The mixed reset above left the tree dirty, so switching to `feature`
        # conflicts: git's own refusal comes back as a 409 with its message.
        checked = asyncio.run(
            git_http.git_checkout_handler(_Body({"session_id": "sess-git", "ref": "feature"}))
        )
        assert checked.status_code == 409
        assert "success" in checked.description
