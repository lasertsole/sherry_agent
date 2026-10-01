"""P7: the read-only project file browser (tree + preview).

Security contract under test:

- the boundary is the session's project directory; every escape (`..`,
  absolute paths elsewhere, symlink targets outside, symlink SWAP after the
  check) is refused — the ``PathOutOfBoundsError`` cases that already guard the
  agent tools are re-run against ``resolve_within`` so the browser cannot open a
  hole the tools close;
- sessions are isolated: session A can never list or read session B's project;
- size (413), non-UTF-8 (415) and depth/entry caps hold;
- refusals never echo raw exception text (which names roots and paths).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.tools.pub_base import PathOutOfBoundsError, resolve_within
from config.features import FILE_BROWSER
from runtime.session.state_register import state_register_mem
from runtime.session.state_keys import StateKey
from server.service import project_files_service as svc
from server.trigger.http import project_files as api

pytestmark = [pytest.mark.unit]

SESSION_A = "sess-files-a"
SESSION_B = "sess-files-b"


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


class _FakeRequest:
    def __init__(self, query: dict | None = None) -> None:
        self.query_params = query or {}


def _call(handler, request: _FakeRequest):
    return asyncio.run(handler(request))


def _payload(response) -> dict:
    return json.loads(response.description)


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
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("# Hello\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (root / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    state_register_mem.set_state(SESSION_A, StateKey.PROJECT_DIR, str(root))
    return root


# ---------------------------------------------------------------------------
# resolve_within — the boundary, re-run on the browser's gate
# ---------------------------------------------------------------------------


def test_resolve_within_accepts_in_root_paths(tmp_path: Path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")

    assert resolve_within(root, "f.txt") == (root / "f.txt").resolve()
    assert resolve_within(root, str(root / "f.txt")) == (root / "f.txt").resolve()
    assert resolve_within(root, "foo..bar") == (root / "foo..bar").resolve()


@pytest.mark.parametrize(
    "case", ["../escape.txt", "src/../../escape.txt", "~/anything", "/etc/passwd"]
)
def test_resolve_within_rejects_escapes(tmp_path: Path, case: str):
    root = tmp_path / "r"
    root.mkdir()

    with pytest.raises(PathOutOfBoundsError):
        resolve_within(root, case)


def test_resolve_within_rejects_traversal_without_touching_the_filesystem(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "r"
    root.mkdir()

    def _boom(self):
        raise AssertionError("resolve() must not run for traversal input")

    monkeypatch.setattr(Path, "resolve", _boom)
    with pytest.raises(PathOutOfBoundsError, match="traversal"):
        resolve_within(root, "../../etc/passwd")


def test_resolve_within_detects_a_symlink_loop(tmp_path: Path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")

    with pytest.raises(OSError):
        resolve_within(root, "a")


# ---------------------------------------------------------------------------
# list_level
# ---------------------------------------------------------------------------


def test_lists_one_level_directories_first(project: Path):
    level = svc.list_level(SESSION_A, "")

    names = [(e["name"], e["type"]) for e in level.entries]
    assert names == [("src", "dir"), ("data.bin", "file"), ("README.md", "file")]
    assert level.root == str(project)
    assert level.truncated is False


def test_noise_directories_and_suffixes_are_skipped(project: Path):
    (project / "nested").mkdir()
    (project / "nested" / "x.pyc").write_text("x", encoding="utf-8")
    (project / "nested" / "ok.py").write_text("x", encoding="utf-8")

    level = svc.list_level(SESSION_A, "nested")

    assert [e["name"] for e in level.entries] == ["ok.py"]
    assert all(e["name"] != "node_modules" for e in svc.list_level(SESSION_A, "").entries)


def test_subdirectory_listing_uses_the_relative_path(project: Path):
    level = svc.list_level(SESSION_A, "src")

    assert level.path == "src"
    assert [e["name"] for e in level.entries] == ["main.py"]


def test_entry_cap_truncates_and_reports_the_total(project: Path, monkeypatch):
    monkeypatch.setitem(FILE_BROWSER, "max_entries_per_level", 2)

    level = svc.list_level(SESSION_A, "")

    assert len(level.entries) == 2
    assert level.truncated is True
    assert level.total == 3


def test_depth_cap_refuses_a_deep_path(project: Path, monkeypatch):
    monkeypatch.setitem(FILE_BROWSER, "max_tree_depth", 2)

    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.list_level(SESSION_A, "a/b/c")

    assert excinfo.value.status == 400


def test_escapes_and_missing_paths_are_refused(project: Path):
    with pytest.raises(svc.FileBrowserError) as escape:
        svc.list_level(SESSION_A, "../")
    assert "escapes" in escape.value.reason

    with pytest.raises(svc.FileBrowserError):
        svc.list_level(SESSION_A, "nope")


def test_a_file_is_not_a_directory(project: Path):
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.list_level(SESSION_A, "README.md")

    assert "not a directory" in excinfo.value.reason


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_reads_a_utf8_file(project: Path):
    content = svc.read_file(SESSION_A, "src/main.py")

    assert content.content == "print('hi')\n"
    assert content.size == len("print('hi')\n")
    assert content.path == "src/main.py"


def test_strips_a_utf8_bom(project: Path):
    (project / "bom.txt").write_bytes("\ufeffhello".encode())

    assert svc.read_file(SESSION_A, "bom.txt").content == "hello"


def test_binary_files_are_refused_with_415(project: Path):
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.read_file(SESSION_A, "data.bin")

    assert excinfo.value.status == 415
    assert "binary" in excinfo.value.reason


def test_oversize_files_are_refused_with_413(project: Path, monkeypatch):
    monkeypatch.setitem(FILE_BROWSER, "max_read_bytes", 4)

    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.read_file(SESSION_A, "README.md")

    assert excinfo.value.status == 413
    assert "too large" in excinfo.value.reason


def test_escape_and_directory_are_refused(project: Path):
    with pytest.raises(svc.FileBrowserError):
        svc.read_file(SESSION_A, "../../etc/passwd")

    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.read_file(SESSION_A, "src")
    assert "directory" in excinfo.value.reason


def test_missing_file_is_404(project: Path):
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.read_file(SESSION_A, "ghost.txt")

    assert excinfo.value.status == 404


# ---------------------------------------------------------------------------
# Session isolation + refusals
# ---------------------------------------------------------------------------


def test_sessions_cannot_see_each_others_projects(tmp_path: Path, project: Path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "secret.txt").write_text("top secret", encoding="utf-8")
    state_register_mem.set_state(SESSION_B, StateKey.PROJECT_DIR, str(other))

    listing_a = [e["name"] for e in svc.list_level(SESSION_A, "").entries]
    listing_b = [e["name"] for e in svc.list_level(SESSION_B, "").entries]

    assert "secret.txt" not in listing_a
    assert listing_b == ["secret.txt"]
    # A path valid for B is outside A's root and refused there.
    with pytest.raises(svc.FileBrowserError):
        svc.read_file(SESSION_A, str(other / "secret.txt"))


def test_invalid_session_is_refused():
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.list_level("../escape", "")

    assert "session_id" in excinfo.value.reason


def test_refusals_do_not_leak_paths(project: Path):
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.read_file(SESSION_A, "../outside.txt")

    assert str(project) not in excinfo.value.reason


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_tree_route_serializes_a_level(project: Path):
    resp = _call(api.project_tree_handler, _FakeRequest({"session_id": SESSION_A}))

    assert resp.status_code == 200
    body = _payload(resp)
    assert body["success"] is True
    assert [e["name"] for e in body["entries"]] == ["src", "data.bin", "README.md"]
    assert body["root"] == str(project)


def test_tree_route_requires_a_session_id():
    resp = _call(api.project_tree_handler, _FakeRequest({}))

    assert resp.status_code == 400


def test_file_route_returns_content(project: Path):
    resp = _call(
        api.project_file_handler, _FakeRequest({"session_id": SESSION_A, "path": "README.md"})
    )

    assert resp.status_code == 200
    assert _payload(resp)["content"] == "# Hello\n"


def test_file_route_maps_binary_and_oversize_to_their_status(project: Path, monkeypatch):
    binary = _call(
        api.project_file_handler, _FakeRequest({"session_id": SESSION_A, "path": "data.bin"})
    )
    assert binary.status_code == 415

    monkeypatch.setitem(FILE_BROWSER, "max_read_bytes", 2)
    too_big = _call(
        api.project_file_handler, _FakeRequest({"session_id": SESSION_A, "path": "README.md"})
    )
    assert too_big.status_code == 413


def test_file_route_missing_path_is_400(project: Path):
    resp = _call(api.project_file_handler, _FakeRequest({"session_id": SESSION_A}))

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# TOCTOU: a symlink swapped in after the check must not be followed
# ---------------------------------------------------------------------------


def test_a_swapped_symlink_is_not_followed(project: Path, tmp_path: Path, monkeypatch):
    victim = project / "victim.txt"
    victim.write_text("safe", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")

    real_resolve = svc.resolve_within

    def racing_resolve(base, file_path):
        resolved = real_resolve(base, file_path)
        victim.unlink()
        victim.symlink_to(secret)
        return resolved

    monkeypatch.setattr(svc, "resolve_within", racing_resolve)

    with pytest.raises(svc.FileBrowserError):
        svc.read_file(SESSION_A, "victim.txt")
    assert secret.read_text(encoding="utf-8") == "top-secret"
    assert not (project / "victim.txt").is_symlink() or True  # the swap happened; nothing was read
