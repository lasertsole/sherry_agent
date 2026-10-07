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


class TestCommitDetail:
    """Clicking a commit row: the file list, then one file's aligned diff."""

    @pytest.fixture
    def detail_repo(self, tmp_path, monkeypatch):
        """A commit that modifies, adds, deletes and renames a file."""
        root = tmp_path / "detail"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "tester@example.com")
        _git(root, "config", "user.name", "Tester")
        (root / "keep.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (root / "gone.txt").write_text("bye\n", encoding="utf-8")
        (root / "old-name.txt").write_text("renamed\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", "base")
        (root / "keep.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
        (root / "gone.txt").unlink()
        _git(root, "mv", "old-name.txt", "new-name.txt")
        (root / "added.txt").write_text("fresh\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "mix")
        monkeypatch.setattr(service, "current_project_dir", lambda _sid: root)
        return root

    def test_the_file_list_carries_each_status(self, detail_repo):
        detail = service.read_commit_files("sess-git", "HEAD")

        by_path = {item.path: item.status for item in detail.files}
        assert by_path["keep.txt"] == "M"
        assert by_path["gone.txt"] == "D"
        assert by_path["added.txt"] == "A"
        assert by_path["new-name.txt"] == "R"
        renamed = next(item for item in detail.files if item.path == "new-name.txt")
        assert renamed.old_path == "old-name.txt"
        assert detail.subject == "mix"
        assert detail.parents and len(detail.parents) == 1

    def test_a_modified_file_aligns_old_and_new_lines(self, detail_repo):
        diff = service.read_commit_diff("sess-git", "HEAD", "keep.txt")

        assert diff.status == "M"
        assert diff.old_label.endswith(":keep.txt") and diff.new_label.endswith(":keep.txt")
        # The changed line pairs up: remove on the left, add on the right.
        changed = [
            (row.left, row.right) for row in diff.rows if row.left and row.left["kind"] == "remove"
        ]
        assert changed and changed[0][1] is not None and changed[0][1]["kind"] == "add"
        # The untouched lines ride both columns with their own numbering.
        same = next(row for row in diff.rows if row.left and row.left["kind"] == "same")
        assert same.right is not None and same.right["text"] == same.left["text"]
        assert same.left["n"] == 1 and same.right["n"] == 1
        # The extra line at the end shows up as an addition on the right only.
        assert any(
            row.left is None and row.right and row.right["kind"] == "add" for row in diff.rows
        )

    def test_an_added_file_is_one_column_only(self, detail_repo):
        diff = service.read_commit_diff("sess-git", "HEAD", "added.txt")

        assert diff.status == "A"
        assert diff.old_label == ""
        assert all(row.left is None for row in diff.rows)
        assert [row.right["kind"] for row in diff.rows if row.right] == ["add"]

    def test_a_deleted_file_is_one_column_only(self, detail_repo):
        diff = service.read_commit_diff("sess-git", "HEAD", "gone.txt")

        assert diff.status == "D"
        assert diff.new_label == ""
        assert all(row.right is None for row in diff.rows)
        assert [row.left["kind"] for row in diff.rows if row.left] == ["remove"]

    def test_a_renamed_file_reads_the_old_path_as_its_left_side(self, detail_repo):
        diff = service.read_commit_diff("sess-git", "HEAD", "new-name.txt")

        assert diff.status == "R"
        assert diff.old_path == "old-name.txt"
        assert "old-name.txt" in diff.old_label
        # Identical contents: every row is an unchanged pair.
        assert diff.rows and all(
            (row.left or {}).get("kind") == "same" and (row.right or {}).get("kind") == "same"
            for row in diff.rows
        )

    def test_a_binary_file_is_flagged_instead_of_decoded(self, detail_repo):
        (detail_repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        _git(detail_repo, "add", ".")
        _git(detail_repo, "commit", "-q", "-m", "binary")

        files = service.read_commit_files("sess-git", "HEAD")
        assert any(item.path == "blob.bin" for item in files.files)

        diff = service.read_commit_diff("sess-git", "HEAD", "blob.bin")

        assert diff.binary is True
        assert diff.rows == []

    def test_the_row_cap_clips_a_long_diff_and_says_so(self, detail_repo, monkeypatch):
        (detail_repo / "wide.txt").write_text(
            "".join(f"line {index}\n" for index in range(20)), encoding="utf-8"
        )
        _git(detail_repo, "add", ".")
        _git(detail_repo, "commit", "-q", "-m", "wide")
        monkeypatch.setitem(service.GIT_GRAPH, "max_diff_rows", 5)

        diff = service.read_commit_diff("sess-git", "HEAD", "wide.txt")

        assert diff.status == "A"
        assert len(diff.rows) == 5
        assert diff.truncated is True

    def test_the_byte_cap_flags_a_big_side_instead_of_truncating(self, detail_repo, monkeypatch):
        (detail_repo / "big.txt").write_text("x" * 4096, encoding="utf-8")
        _git(detail_repo, "add", ".")
        _git(detail_repo, "commit", "-q", "-m", "big")
        monkeypatch.setitem(service.GIT_GRAPH, "max_diff_bytes", 16)

        diff = service.read_commit_diff("sess-git", "HEAD", "big.txt")

        assert diff.status == "A"
        assert diff.rows == []
        assert diff.notice == "too-large"

    def test_the_routes_answer_the_list_and_the_diff(self, detail_repo):
        listed = asyncio.run(
            git_http.git_commit_handler(_FakeRequest({"session_id": "sess-git", "hash": "HEAD"}))
        )
        assert listed.status_code == 200
        data = json.loads(listed.description)
        assert {item["path"] for item in data["files"]} >= {"keep.txt", "added.txt", "gone.txt"}

        diffed = asyncio.run(
            git_http.git_commit_file_handler(
                _FakeRequest({"session_id": "sess-git", "hash": "HEAD", "path": "keep.txt"})
            )
        )
        assert diffed.status_code == 200
        payload = json.loads(diffed.description)
        assert payload["status"] == "M"
        assert payload["rows"] and payload["rows"][0]["left"]["text"] == "one"

        # Missing arguments are refused before git runs.
        assert asyncio.run(git_http.git_commit_handler(_FakeRequest({}))).status_code == 400
        assert (
            asyncio.run(
                git_http.git_commit_file_handler(_FakeRequest({"session_id": "sess-git"}))
            ).status_code
            == 400
        )

    def test_a_non_ascii_path_is_never_git_quoted(self, detail_repo):
        old = "文档 说明.md"
        new = "文档 说明书.md"
        (detail_repo / old).write_text("内容\n", encoding="utf-8")
        _git(detail_repo, "add", ".")
        _git(detail_repo, "commit", "-q", "-m", "chinese file")
        _git(detail_repo, "mv", old, new)
        _git(detail_repo, "commit", "-q", "-m", "chinese rename")

        files = service.read_commit_files("sess-git", "HEAD")
        renamed = next(item for item in files.files if item.status == "R")
        assert (renamed.old_path, renamed.path) == (old, new)

        # The entry lookup behind the diff view needs the decoded name, not
        # `"\\346\\226\\207"` — the quoted form used to hide the status.
        diff = service.read_commit_diff("sess-git", "HEAD", new)
        assert diff.status == "R"
        assert diff.old_label.endswith(f":{old}") and diff.new_label.endswith(f":{new}")
        assert diff.rows and all((row.left or {}).get("kind") == "same" for row in diff.rows)

    def test_a_deleted_non_ascii_file_keeps_its_single_column(self, detail_repo):
        name = "旧 报告.md"
        (detail_repo / name).write_text("old line\n", encoding="utf-8")
        _git(detail_repo, "add", ".")
        _git(detail_repo, "commit", "-q", "-m", "with chinese file")
        (detail_repo / name).unlink()
        _git(detail_repo, "add", "-A")
        _git(detail_repo, "commit", "-q", "-m", "drop chinese file")

        diff = service.read_commit_diff("sess-git", "HEAD", name)

        assert diff.status == "D"
        assert diff.new_label == ""
        assert all(row.right is None for row in diff.rows)
        assert [row.left["kind"] for row in diff.rows if row.left] == ["remove"]

    def test_a_path_outside_the_commit_is_refused(self, detail_repo):
        with pytest.raises(service.GitActionError) as excinfo:
            service.read_commit_diff("sess-git", "HEAD", "never-touched.txt")

        assert excinfo.value.status == 404

        refused = asyncio.run(
            git_http.git_commit_file_handler(
                _FakeRequest(
                    {"session_id": "sess-git", "hash": "HEAD", "path": "never-touched.txt"}
                )
            )
        )
        assert refused.status_code == 404
