"""Git readiness before writes (``pub_base/git_ensure``).

The rule under test: a session with a BOUND project directory may only write
into a git repository — the first write initializes one, a failed ``git init``
refuses the write with a readable message, and a session WITHOUT a binding is
left exactly as before. All four write doors are gated (``write_file`` /
``patch_file`` / ``terminal`` / ``python_repl``), while read operations stay
untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent.tools.pub_base import git_ensure
from agent.tools.pub_base.git_ensure import (
    clear_git_ready,
    ensure_git_for_writes,
    git_ready_roots,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-git-ready"
OTHER = "s-git-ready-other"


@pytest.fixture(autouse=True)
def _clean_cache():
    """The cache is process-level: never let one test see another's roots."""
    clear_git_ready()
    yield
    clear_git_ready()


@pytest.fixture()
def bind(monkeypatch):
    """Bind a session to a directory (the state register's PROJECT_DIR key)."""
    from runtime import state_register_mem
    from runtime import StateKey

    monkeypatch.setattr(state_register_mem, "_states", {})

    def _bind(session: str, directory: Path) -> Path:
        state_register_mem.set_state(session, StateKey.PROJECT_DIR, str(directory))
        return directory

    return _bind


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


class TestTheGate:
    def test_no_binding_means_no_check_at_all(self, monkeypatch, tmp_path: Path):
        calls: list[object] = []
        monkeypatch.setattr(git_ensure.subprocess, "run", lambda *a, **k: calls.append(a))

        assert ensure_git_for_writes("") == (True, "")
        assert ensure_git_for_writes("unbound-session") == (True, "")
        assert calls == [], "an unbound session must not even probe"

    def test_an_existing_repo_is_reused_without_init(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init")
        bind(SESSION, root)
        inits: list[object] = []
        monkeypatch.setattr(git_ensure, "_git_init", lambda path: inits.append(path) or (True, ""))

        assert ensure_git_for_writes(SESSION) == (True, "")
        assert inits == [], "an existing repository is reused as-is"

    def test_a_subdirectory_of_a_repo_counts(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "repo"
        (root / "pkg").mkdir(parents=True)
        _git(root, "init")
        bind(SESSION, root / "pkg")
        inits: list[object] = []
        monkeypatch.setattr(git_ensure, "_git_init", lambda path: inits.append(path) or (True, ""))

        assert ensure_git_for_writes(SESSION) == (True, "")
        assert inits == [], "a work-tree subdirectory is inside the repository"

    def test_a_plain_directory_is_initialized(self, bind, tmp_path: Path):
        root = tmp_path / "plain"
        root.mkdir()
        bind(SESSION, root)
        assert not (root / ".git").exists()

        assert ensure_git_for_writes(SESSION) == (True, "")

        assert (root / ".git").exists(), "the first write initialized the repository"

    def test_a_git_worktree_passes(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init")
        _git(root, "commit", "--allow-empty", "-m", "seed")
        linked = tmp_path / "linked"
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", str(linked)],
            check=True,
            capture_output=True,
            text=True,
        )
        bind(SESSION, linked)
        inits: list[object] = []
        monkeypatch.setattr(git_ensure, "_git_init", lambda path: inits.append(path) or (True, ""))

        assert ensure_git_for_writes(SESSION) == (True, "")
        assert inits == [], "an isolated worktree is a git working tree already"

    def test_a_failed_init_refuses_and_names_the_directory(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "locked"
        root.mkdir()
        bind(SESSION, root)
        monkeypatch.setattr(git_ensure, "_git_init", lambda path: (False, "read-only filesystem"))

        ok, error = ensure_git_for_writes(SESSION)

        assert ok is False
        assert str(root) in error and "read-only filesystem" in error
        assert "refused" in error

    def test_a_real_read_only_directory_is_refused(self, bind, tmp_path: Path):
        # The real thing, not a fake: `git init` needs to create `.git/`, so an
        # unwritable directory fails for real (skipped when the test runs as
        # root or on a filesystem that ignores the mode).
        root = tmp_path / "locked-real"
        root.mkdir()
        root.chmod(0o500)
        bind(SESSION, root)
        try:
            ok, error = ensure_git_for_writes(SESSION)
        finally:
            root.chmod(0o700)
        if ok:  # pragma: no cover - root-run hosts
            pytest.skip("git init succeeded as root")
        assert "Git initialization failed" in error
        assert str(root) in error

    def test_git_missing_is_reported_as_such(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "no-git"
        root.mkdir()
        bind(SESSION, root)

        def _boom(*_args, **_kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(git_ensure.subprocess, "run", _boom)

        ok, error = ensure_git_for_writes(SESSION)

        assert ok is False
        assert "not installed" in error

    def test_a_missing_directory_is_left_to_the_write_tool(self, bind, tmp_path: Path):
        bind(SESSION, tmp_path / "does-not-exist")

        assert ensure_git_for_writes(SESSION) == (True, "")


class TestTheCache:
    def test_a_verified_root_probes_once(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init")
        bind(SESSION, root)
        probes: list[object] = []
        real_run = subprocess.run

        def _spy(*args, **kwargs):
            probes.append(args)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(git_ensure.subprocess, "run", _spy)

        assert ensure_git_for_writes(SESSION) == (True, "")
        assert ensure_git_for_writes(SESSION) == (True, "")
        assert len(probes) == 1, "the second call must hit the cache"
        assert str(root) in git_ready_roots()

    def test_switching_the_bound_directory_re_probes(self, bind, tmp_path: Path):
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        bind(SESSION, first)

        assert ensure_git_for_writes(SESSION) == (True, "")
        bind(SESSION, second)
        assert ensure_git_for_writes(SESSION) == (True, "")

        assert (first / ".git").exists() and (second / ".git").exists()

    def test_a_failed_init_is_never_cached(self, bind, tmp_path: Path, monkeypatch):
        root = tmp_path / "flaky"
        root.mkdir()
        bind(SESSION, root)
        attempts: list[int] = []

        def _fail_then_succeed(path: Path):
            attempts.append(1)
            if len(attempts) == 1:
                return False, "transient failure"
            (path / ".git").mkdir(exist_ok=True)
            return True, ""

        monkeypatch.setattr(git_ensure, "_git_init", _fail_then_succeed)

        assert ensure_git_for_writes(SESSION)[0] is False
        assert ensure_git_for_writes(SESSION) == (True, "")
        assert len(attempts) == 2, "the retry re-ran the init"


class TestTheToolsRefuse:
    """Every write door refuses when the project root cannot get git."""

    @pytest.fixture()
    def failing_init(self, monkeypatch):
        monkeypatch.setattr(git_ensure, "_git_init", lambda path: (False, "simulated failure"))

    def test_write_file_refuses_and_writes_nothing(self, bind, tmp_path: Path, failing_init):
        from agent.tools.file_tools.write_file import build_write_file_tool

        root = tmp_path / "project"
        root.mkdir()
        bind(SESSION, root)

        out = build_write_file_tool()._core("note.txt", "hello", session_id=SESSION)

        assert out.startswith("Error:") or json.loads(out).get("error")
        assert "Git initialization failed" in out
        assert not (root / "note.txt").exists(), "a refused write must not create the file"

    def test_patch_file_refuses_and_leaves_the_file_alone(self, bind, tmp_path: Path, failing_init):
        from agent.tools.file_tools.patch_file import build_patch_file_tool

        root = tmp_path / "project"
        root.mkdir()
        target = root / "app.py"
        target.write_text("value = 1\n", encoding="utf-8")
        bind(SESSION, root)

        out = build_patch_file_tool()._core("app.py", "value = 1", "value = 2", session_id=SESSION)

        assert "Git initialization failed" in out
        assert target.read_text(encoding="utf-8") == "value = 1\n", "the patch did not land"

    def test_terminal_refuses_to_run(self, bind, tmp_path: Path, failing_init):
        from agent.tools.terminal import build_terminal_tool

        root = tmp_path / "project"
        root.mkdir()
        bind(SESSION, root)

        out = build_terminal_tool()._run(["echo hi > side.txt"], session_id=SESSION)

        assert "Git initialization failed" in out
        assert not (root / "side.txt").exists(), "no shell command may run"

    def test_python_repl_refuses_to_run(self, bind, tmp_path: Path, failing_init):
        from agent.tools.python_repl import build_python_repl_tool

        root = tmp_path / "project"
        root.mkdir()
        bind(SESSION, root)

        out = build_python_repl_tool()._run("open('side.txt', 'w').write('x')", session_id=SESSION)

        assert "Git initialization failed" in out
        assert not (root / "side.txt").exists(), "no python code may run"


class TestTheWritePathEndToEnd:
    def test_a_bound_plain_directory_gets_git_then_the_write_lands(self, bind, tmp_path: Path):
        """The whole requirement, through the real tool with real git."""
        from agent.tools.file_tools.write_file import build_write_file_tool

        root = tmp_path / "fresh-project"
        root.mkdir()
        bind(SESSION, root)
        tool = build_write_file_tool()

        first = tool._core("src/app.py", "print('hi')\n", session_id=SESSION)

        assert "written successfully" in first, first
        assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert (root / ".git").is_dir(), "the write initialized the repository"

        # The second write rides the cache and still lands.
        second = tool._core("README.md", "hello\n", session_id=SESSION)
        assert "written successfully" in second, second
        assert (root / "README.md").exists()


class TestReadsAreUntouched:
    def test_read_operations_never_initialize_git(self, bind, tmp_path: Path):
        from agent.tools.file_tools.read_file import build_read_file_tool

        root = tmp_path / "library"
        root.mkdir()
        target = root / "data.txt"
        target.write_text("content\n", encoding="utf-8")
        bind(SESSION, root)

        out = json.loads(build_read_file_tool()._core("data.txt", session_id=SESSION))

        assert out["content"].endswith("content")
        assert not (root / ".git").exists(), "reading a plain directory must not init git"
