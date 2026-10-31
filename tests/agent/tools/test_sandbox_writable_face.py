"""The sandbox's WRITE surface is the session's project directory, not ROOT_DIR.

P1 of the bypass-defense plan. The sandbox is the only write barrier terminal /
python_repl / ptc have, and its writable面 used to be the hardcoded ROOT_DIR —
which both refused a project bound outside the repository (functional bug) and
granted write access to all of the checkout (a way around the external-path
gate). These tests pin the new contract at each layer:

* the backends bind ONLY what the caller passed (+ TEMP_DIR),
* the three tools pass their session's project root,
* an unbound session keeps the pre-existing behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base import sandbox as sandbox_mod
from agent.tools.pub_base.sandbox import normalize_writable_roots
from agent.tools.pub_base.sandbox_bwrap import BwrapBackend, _writable_paths
from runtime.session.state_register import state_register_mem
from runtime.session.state_keys import StateKey

pytestmark = [pytest.mark.unit]

SESSION = "sess-writable"


class _RecordingBackend:
    """A backend that records the write surface it was handed."""

    def __init__(self) -> None:
        self.roots: list[list[Path]] = []

    def probe(self) -> bool:
        return True

    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: list[Path],
        *,
        allow_network: bool = False,
    ):
        self.roots.append(list(writable_roots))
        return ["sandboxed", *cmd], env


@pytest.fixture()
def project_root(tmp_path: Path, monkeypatch):
    """A project directory OUTSIDE the repository (the bug's trigger)."""
    root = tmp_path / "user-project"
    root.mkdir()
    state_register_mem.set_state(SESSION, StateKey.PROJECT_DIR, str(root))
    yield root
    state_register_mem.clear_session(SESSION)


class TestNormalizeWritableRoots:
    def test_empty_is_a_caller_bug(self):
        with pytest.raises(ValueError):
            normalize_writable_roots([])

    def test_temp_dir_is_always_present(self):
        roots = normalize_writable_roots([Path("/tmp/some-project")])

        assert Path(sandbox_mod.TEMP_DIR).resolve() in roots

    def test_a_missing_temp_dir_is_created_for_the_bind_mount(self, tmp_path, monkeypatch):
        """bwrap cannot bind a source that does not exist (fresh checkout / CI)."""
        missing = tmp_path / "runtime-temp"
        monkeypatch.setattr(sandbox_mod, "TEMP_DIR", missing)
        assert not missing.exists()

        roots = normalize_writable_roots([tmp_path / "project"])

        assert missing.is_dir()
        assert missing.resolve() in roots

    def test_duplicates_collapse(self, tmp_path):
        roots = normalize_writable_roots([tmp_path, tmp_path / "."])

        assert roots.count(tmp_path.resolve()) == 1


class TestBwrapBindsOnlyTheCallersRoots:
    def test_argv_does_not_contain_root_dir(self, tmp_path):
        """The regression this contract exists for: ROOT_DIR is never bound."""
        cmds = _writable_paths([tmp_path])

        assert str(tmp_path.resolve()) in cmds
        from config.path import ROOT_DIR

        assert str(ROOT_DIR) not in cmds

    def test_bind_count_matches_the_roots_plus_temp(self, tmp_path):
        cmd, _env = BwrapBackend().wrap(["echo"], {}, [tmp_path])

        from config.path import TEMP_DIR

        binds = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--bind"]
        assert binds == [str(tmp_path.resolve()), str(TEMP_DIR.resolve())]


class TestTerminalPassesTheSessionRoot:
    def test_bound_session_gets_its_project_dir(self, project_root: Path, monkeypatch):
        from agent.tools.terminal import build_terminal_tool

        backend = _RecordingBackend()
        monkeypatch.setattr("agent.tools.terminal.core.get_backend", lambda _policy: backend)
        monkeypatch.setattr(
            "agent.tools.terminal.core.read_policy", lambda: sandbox_mod.SandboxPolicy.REQUIRED
        )
        tool = build_terminal_tool()

        roots = tool._session_writable_roots(SESSION)

        assert roots and roots[0] == project_root
        assert all("sherry_agent" not in str(root) or root == project_root for root in roots)

    def test_unbound_session_falls_back_to_the_process_root(self):
        from agent.tools.terminal import build_terminal_tool

        tool = build_terminal_tool()

        roots = tool._session_writable_roots("")

        # No binding → the historical ROOT_DIR surface (unchanged behaviour).
        assert len(roots) == 1


class TestPythonReplPassesTheSessionRoot:
    def test_the_repl_wrap_receives_the_session_root(self, project_root: Path, monkeypatch):
        from agent.tools import python_repl

        backend = _RecordingBackend()
        monkeypatch.setattr(python_repl, "get_backend", lambda _policy: backend)
        monkeypatch.setattr(python_repl, "read_policy", lambda: sandbox_mod.SandboxPolicy.REQUIRED)

        class _Proc:
            returncode = 0

            def __init__(self, **_kwargs: object) -> None:
                pass

            def wait(self, timeout=None):  # fd-direct-write: the flow waits on the child
                return self.returncode

            def communicate(self, timeout=None):  # pragma: no cover
                raise AssertionError("the fd-direct-write path must not drain a pipe")

        monkeypatch.setattr(python_repl.subprocess, "Popen", lambda *_a, **_kw: _Proc())

        python_repl._run_with_timeout("print(1)", 5, True, str(project_root), [project_root])

        assert backend.roots == [[project_root]]


class TestPtcPassesItsCwd:
    def test_the_child_write_surface_is_its_cwd(self, tmp_path: Path, monkeypatch):
        from agent.tools.ptc import runner as ptc_runner

        backend = _RecordingBackend()
        monkeypatch.setattr(ptc_runner, "get_backend", lambda _policy: backend)
        monkeypatch.setattr(ptc_runner, "read_policy", lambda: sandbox_mod.SandboxPolicy.REQUIRED)

        ptc_runner._resolve_sandboxed_argv(["python", "s.py"], {}, [tmp_path])

        assert backend.roots == [[tmp_path]]
