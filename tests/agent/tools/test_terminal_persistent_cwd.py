"""The persistent-cwd wrapper: ``cd``/``export`` survive between commands.

A fresh shell per call used to throw the working directory and exported
variables away after every command (the eval harness's DockerBridge already
restores cwd+env inside containers with two state files; this is the same
mechanism for the local path). Pinned:

* the wrapper is only built for a POSIX-capable shell with a safe session id —
  no session, an unsafe id, or the cmd.exe path leaves the command unchanged;
* the generated script restores cwd + env, runs the command once, preserves
  its exit code, and persists ``pwd`` + ``export -p`` — silently (a broken
  state file degrades to the fresh-shell behavior, never to a failed command);
* end to end through a REAL shell: a ``cd`` in one command is where the next
  one runs, an ``export`` in one command is visible to the next, and the exit
  code travels through the wrapper.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

import agent.tools.pub_base.sandbox_guard as sandbox_guard
import agent.tools.terminal.core as terminal
from agent.tools.pub_base.sandbox import SandboxPolicy
from agent.tools.terminal.core import _terminal_state_files, _wrap_persistent_shell
from agent.tools.terminal import SafeShellTool

pytestmark = [
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]


@pytest.fixture
def sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import config
    import pub.func.message.eviction as eviction_module

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(config, "SESSIONS_DIR", root)
    monkeypatch.setattr(eviction_module, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> SafeShellTool:
    monkeypatch.setattr(sandbox_guard, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
    monkeypatch.setattr(terminal, "scrub_env", lambda base_env=None: dict(os.environ))
    return SafeShellTool()


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A bound project directory so commands run outside the checkout."""
    root = tmp_path / "proj"
    root.mkdir()
    from runtime.session.project_dir import write_project_dir

    sid = f"persist-{uuid.uuid4().hex[:8]}"
    write_project_dir(sid, root)
    yield root
    from runtime.session.project_dir import read_project_dir

    if read_project_dir(sid) is not None:
        from runtime.session.project_dir import write_pending_project_dir

        write_pending_project_dir(sid, None)


# ─────────────────────────────────────────────────────────────────────────────
# The wrapper builder
# ─────────────────────────────────────────────────────────────────────────────
class TestWrapperBuilder:
    @pytest.mark.unit
    def test_no_session_leaves_the_command_alone(self):
        cmd = "npm test"
        assert _wrap_persistent_shell(cmd, "") == cmd

    @pytest.mark.unit
    def test_unsafe_session_id_leaves_the_command_alone(self):
        cmd = "npm test"
        assert _wrap_persistent_shell(cmd, "../evil") == cmd

    @pytest.mark.unit
    def test_state_files_are_refused_outside_sessions_dir(self, sessions_root: Path):
        assert _terminal_state_files("") is None
        assert _terminal_state_files("../evil") is None

    @pytest.mark.unit
    def test_the_wrapper_restores_runs_and_persists(self, sessions_root: Path):
        wrapped = _wrap_persistent_shell("npm test", "persist-x")
        assert wrapped != "npm test"
        assert "npm test" in wrapped
        assert "set +e" in wrapped
        assert "terminal-state" in wrapped
        # restore → run → capture, in that order (state paths may be quoted or
        # bare depending on the platform: a POSIX path with no spaces is bare)
        assert wrapped.index(". ") < wrapped.index("npm test") < wrapped.index("pwd > ")
        assert wrapped.rstrip().endswith("exit $__sherry_rc")
        # every state write is silent
        assert "2>/dev/null" in wrapped


# ─────────────────────────────────────────────────────────────────────────────
# End to end through a real shell (Git Bash on Windows, /bin/sh on POSIX)
# ─────────────────────────────────────────────────────────────────────────────
class TestPersistentShellE2E:
    @pytest.mark.integration
    def test_a_cd_persists_into_the_next_command(
        self, tool: SafeShellTool, sessions_root: Path, project: Path
    ):
        sid = f"persist-{uuid.uuid4().hex[:8]}"

        first = tool._run(["mkdir -p nested && cd nested && pwd"], session_id=sid)
        assert "nested" in first
        second = tool._run(["pwd"], session_id=sid)
        assert second.strip().endswith("nested"), second

    @pytest.mark.integration
    def test_an_export_persists_into_the_next_command(
        self, tool: SafeShellTool, sessions_root: Path, project: Path
    ):
        sid = f"persist-{uuid.uuid4().hex[:8]}"

        tool._run(["export SHERRY_PERSIST_PROBE=hello-persist"], session_id=sid)
        out = tool._run(["echo $SHERRY_PERSIST_PROBE"], session_id=sid)
        assert "hello-persist" in out

    @pytest.mark.integration
    def test_the_exit_code_travels_through_the_wrapper(
        self, tool: SafeShellTool, sessions_root: Path, project: Path
    ):
        sid = f"persist-{uuid.uuid4().hex[:8]}"

        out = tool._run(["exit 3"], session_id=sid)
        assert "Exit code 3" in out

    @pytest.mark.integration
    def test_state_files_land_in_the_session_dir(
        self, tool: SafeShellTool, sessions_root: Path, project: Path
    ):
        sid = f"persist-{uuid.uuid4().hex[:8]}"

        tool._run(["true"], session_id=sid)
        state = sessions_root / sid / "terminal-state"
        assert (state / "cwd").is_file()
        assert (state / "env").is_file()

    @pytest.mark.integration
    def test_a_safe_session_id_wraps_even_without_a_project_binding(
        self, tool: SafeShellTool, sessions_root: Path
    ):
        """Persistence keys on the SESSION, not on a project binding: an
        unbound session runs at the process default root and still keeps its
        own cwd/env between commands."""
        sid = f"persist-{uuid.uuid4().hex[:8]}"
        out = tool._run(["pwd"], session_id=sid)
        assert out.strip()
        assert (sessions_root / sid / "terminal-state" / "cwd").is_file()
