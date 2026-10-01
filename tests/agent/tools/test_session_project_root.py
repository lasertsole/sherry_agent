"""P3: every execution point resolves against the SESSION's project directory.

The tools are process-level singletons (``agent/core.py`` builds them once), so
any construction-time capture of a root would freeze the whole process. These
tests lock the observable consequences:

- resolution follows the session binding, and follows a CHANGED binding within
  one process (the singleton trap);
- two sessions bound to different roots never see each other's files;
- an unbound session keeps the historical repo-root behaviour;
- the path guard moves its boundary with the session but keeps its policy;
- terminal / python_repl / ptc / ast-grep all read the same value per call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.pub_base import session_workspace_root
from agent.tools.pub_base import path_utils
from runtime.session.state_register import state_register_mem
from runtime.session.state_keys import StateKey

pytestmark = [pytest.mark.unit]

SESSION_A = "sess-root-a"
SESSION_B = "sess-root-b"


class _InMemoryRegisterDB:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.store.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.store[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.store.pop((session_id, key), None)
        return True


@pytest.fixture(autouse=True)
def _isolated_register(monkeypatch):
    import runtime

    monkeypatch.setattr(runtime, "state_register_db", _InMemoryRegisterDB())
    for sid in (SESSION_A, SESSION_B):
        state_register_mem.clear_session(sid)
    yield
    for sid in (SESSION_A, SESSION_B):
        state_register_mem.clear_session(sid)


@pytest.fixture()
def two_roots(tmp_path: Path) -> tuple[Path, Path]:
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    for root in (a, b):
        root.mkdir()
        (root / "note.txt").write_text(f"hello from {root.name}", encoding="utf-8")
    return a, b


def _bind(session_id: str, root: Path) -> None:
    state_register_mem.set_state(session_id, StateKey.PROJECT_DIR, str(root))


# ---------------------------------------------------------------------------
# The accessor
# ---------------------------------------------------------------------------


def test_unbound_session_has_no_root(two_roots: tuple[Path, Path]):
    assert session_workspace_root(SESSION_A) is None
    assert session_workspace_root("") is None
    assert session_workspace_root(None) is None


def test_bound_session_reports_its_root(two_roots: tuple[Path, Path]):
    a, _b = two_roots
    _bind(SESSION_A, a)

    assert session_workspace_root(SESSION_A) == a


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


def test_read_file_reads_inside_the_bound_root(two_roots: tuple[Path, Path]):
    from agent.tools.file_tools.read_file import build_read_file_tool

    a, _b = two_roots
    _bind(SESSION_A, a)

    result = json.loads(build_read_file_tool()._core("note.txt", session_id=SESSION_A))

    assert "hello from proj-a" in result["content"]


def test_read_file_refuses_the_repo_when_bound_elsewhere(two_roots: tuple[Path, Path]):
    """No silent fallback: a repo-relative path must not resolve once bound."""
    from agent.tools.file_tools.read_file import build_read_file_tool

    a, _b = two_roots
    _bind(SESSION_A, a)

    result = json.loads(build_read_file_tool()._core("README.md", session_id=SESSION_A))

    # Either the external-approval gate refuses it (no HITL in this call) or the
    # path simply does not exist inside the bound root — never the repo's file.
    assert "error" in result
    assert "ZCode" not in result.get("content", "")


def test_two_sessions_resolve_against_their_own_roots(two_roots: tuple[Path, Path]):
    """The lock for the singleton trap: same process, two roots, no cross-talk."""
    from agent.tools.file_tools.read_file import build_read_file_tool

    a, b = two_roots
    _bind(SESSION_A, a)
    _bind(SESSION_B, b)
    tool = build_read_file_tool()

    first = json.loads(tool._core("note.txt", session_id=SESSION_A))
    second = json.loads(tool._core("note.txt", session_id=SESSION_B))

    assert "proj-a" in first["content"]
    assert "proj-b" in second["content"]


def test_resolution_follows_a_changed_binding_in_one_process(two_roots: tuple[Path, Path]):
    from agent.tools.file_tools.read_file import build_read_file_tool

    a, b = two_roots
    tool = build_read_file_tool()

    _bind(SESSION_A, a)
    assert "proj-a" in json.loads(tool._core("note.txt", session_id=SESSION_A))["content"]
    _bind(SESSION_A, b)
    assert "proj-b" in json.loads(tool._core("note.txt", session_id=SESSION_A))["content"]


def test_write_file_reports_a_virtual_path_within_the_bound_root(two_roots: tuple[Path, Path]):
    from agent.tools.file_tools.write_file import build_write_file_tool

    a, _b = two_roots
    _bind(SESSION_A, a)

    out = build_write_file_tool()._core("deep/new.txt", "x", session_id=SESSION_A)

    assert "deep/new.txt" in out
    assert (a / "deep" / "new.txt").read_text(encoding="utf-8") == "x"


def test_unbound_session_keeps_the_repo_root(tmp_path: Path, monkeypatch):
    """Nothing bound -> the process default (here: the env override) applies."""
    root = tmp_path / "process-default"
    root.mkdir()
    (root / "note.txt").write_text("default", encoding="utf-8")
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))
    from agent.tools.file_tools.read_file import build_read_file_tool

    result = json.loads(build_read_file_tool()._core("note.txt", session_id=SESSION_A))

    assert "default" in result["content"]


# ---------------------------------------------------------------------------
# Terminal / python_repl / ptc
# ---------------------------------------------------------------------------


def test_terminal_resolves_the_session_root(two_roots: tuple[Path, Path]):
    from agent.tools.terminal import build_terminal_tool

    a, _b = two_roots
    _bind(SESSION_A, a)
    tool = build_terminal_tool()

    assert tool._resolve_cwd(SESSION_A) == str(a)
    assert tool._resolve_cwd(SESSION_B) != str(a), "an unbound session differs"


def test_terminal_without_a_session_uses_the_constructor_fallback(two_roots):
    from agent.tools.terminal import build_terminal_tool
    from config import ROOT_DIR

    tool = build_terminal_tool()

    assert tool._resolve_cwd(None) == str(ROOT_DIR)


def test_python_repl_resolves_the_session_root(two_roots: tuple[Path, Path]):
    from agent.tools import python_repl

    a, _b = two_roots
    _bind(SESSION_A, a)

    class _RunManager:
        def __init__(self, sid: str) -> None:
            self._sid = sid

        def _extract(self) -> str:
            return self._sid

    # The helper reads the session id through _extract_session_id(run_manager);
    # stub that seam instead of fabricating a whole callback manager.
    import agent.tools.python_repl as mod

    original = mod._extract_session_id
    mod._extract_session_id = lambda _rm: SESSION_A
    try:
        assert python_repl._resolve_session_cwd(_RunManager(SESSION_A)) == str(a)
        mod._extract_session_id = lambda _rm: ""
        assert python_repl._resolve_session_cwd(_RunManager("")) is None
    finally:
        mod._extract_session_id = original


def test_ptc_resolves_the_session_root(two_roots: tuple[Path, Path]):
    from agent.tools.ptc.tool import build_ptc_tool

    a, _b = two_roots
    _bind(SESSION_A, a)

    tool = build_ptc_tool(available_tools=[], session_id=SESSION_A)

    assert tool._resolve_cwd() == str(a)


# ---------------------------------------------------------------------------
# PathGuard: boundary follows the session, policy unchanged
# ---------------------------------------------------------------------------


def test_path_guard_accepts_in_session_root_paths(two_roots: tuple[Path, Path]):
    from agent.middlewares.path_guard import core as guard

    a, _b = two_roots

    assert guard._screen_path_arg(str(a / "note.txt"), a) is None
    assert guard._screen_path_arg("note.txt", a) is None


def test_path_guard_still_rejects_traversal_and_credentials(two_roots: tuple[Path, Path]):
    from agent.middlewares.path_guard import core as guard

    a, _b = two_roots

    assert guard._screen_path_arg("../../etc/passwd", a) is not None
    assert guard._screen_path_arg("/etc/passwd", a) is not None


def test_path_guard_reads_the_session_from_the_graph_state(two_roots: tuple[Path, Path]):
    from agent.middlewares.path_guard.core import _session_project_root

    a, _b = two_roots
    _bind(SESSION_A, a)

    class _Request:
        state = {"session_id": SESSION_A}

    class _Stateless:
        state = {}

    assert _session_project_root(_Request()) == a
    assert _session_project_root(_Stateless()) is None


# ---------------------------------------------------------------------------
# code-intel
# ---------------------------------------------------------------------------


def test_ast_grep_resolves_inside_the_session_root(two_roots: tuple[Path, Path]):
    from agent.tools.code_intel.ast_grep import runner as sg

    a, _b = two_roots
    resolved, error = sg._resolve_one_path("note.txt", a)

    assert error is None
    assert resolved == (a / "note.txt").resolve()

    _, outside = sg._resolve_one_path("../escape.txt", a)
    assert outside is not None, "escapes stay rejected"


def test_ast_grep_tools_expose_the_session_root(two_roots: tuple[Path, Path]):
    from agent.tools.code_intel.ast_grep.runner import build_ast_grep_tools

    a, _b = two_roots
    _bind(SESSION_A, a)

    for tool in build_ast_grep_tools(SESSION_A):
        assert tool._session_root() == a


def test_lsp_resolves_inside_the_session_root(two_roots: tuple[Path, Path]):
    from agent.tools.code_intel.lsp import tools as lsp

    a, _b = two_roots
    resolved, error = lsp._resolve_file("note.txt", a)

    assert error is None
    assert resolved == (a / "note.txt").resolve()


def test_path_utils_rendering_anchors_at_the_session_root(two_roots: tuple[Path, Path]):
    a, _b = two_roots

    assert path_utils.display_path(a / "note.txt", a) == "/note.txt"
    # Without the root argument the historical ROOT_DIR anchor applies.
    assert path_utils.display_path(a / "note.txt") == "note.txt"


# ---------------------------------------------------------------------------
# InjectedState is the PRODUCTION channel for subprocess tools
# ---------------------------------------------------------------------------


def test_terminal_takes_the_session_from_the_injected_state(two_roots):
    """The runnable-config lookup is empty in production (smoke finding).

    `_extract_session_id(run_manager)` reads `config["configurable"]["session_id"]`,
    which nothing writes at runtime — so a terminal call that relied on it alone
    ran in the repository root instead of the session's project directory. The
    schema carries `session_id` as `InjectedState`, and the resolved cwd must
    come from it whenever the fallback is empty.
    """
    from agent.tools.terminal import SafeShellInput, build_terminal_tool

    a, _b = two_roots
    _bind(SESSION_A, a)
    tool = build_terminal_tool()

    assert "session_id" in SafeShellInput.model_fields
    assert tool._resolve_cwd(SESSION_A) == str(a)

    class _EmptyRunManager:
        """Stands in for the empty runnable config: no session id anywhere."""

        def __getattr__(self, name):
            return None

    # The fallback path stays intact for direct callers…
    assert tool._resolve_cwd(None) != str(a)
    # …but the injected value always wins in production.
    assert tool._resolve_cwd(SESSION_A) == str(a)


def test_python_repl_prefers_the_injected_session(two_roots, monkeypatch):
    from agent.tools import python_repl

    a, _b = two_roots
    _bind(SESSION_A, a)

    monkeypatch.setattr(python_repl, "_extract_session_id", lambda _rm: "")
    assert python_repl._resolve_session_cwd(None, SESSION_A) == str(a)
    assert python_repl._resolve_session_cwd(None, "") is None
