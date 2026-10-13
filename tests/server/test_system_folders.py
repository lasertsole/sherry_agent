"""The system folder picker (``GET /system/dirs``): directories only, bounded.

Contract under test:

- the listing answers direct **directories** of one absolute path — never files,
  never contents (the picker needs names to navigate, nothing else);
- navigation facts are exact: ``parent`` is ``None`` at the filesystem root, so
  the UI's "go up" has a defined stop, and entries carry absolute ``path``s;
- refusals are client-safe: a relative path, a missing directory and a file are
  rejected with 400/404 rather than an exception or a silent empty list;
- the per-level cap and the empty-path default (the server user's home) hold.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from config.features import FILE_BROWSER
from server.service import system_folders_service as svc
from server.trigger.http import project_files as api

pytestmark = [pytest.mark.unit]


class _FakeRequest:
    def __init__(self, query: dict | None = None) -> None:
        self.query_params = query or {}


def _call(handler, request: _FakeRequest):
    return asyncio.run(handler(request))


def _payload(response) -> dict:
    return json.loads(response.description)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A level with two dirs, one file, and a hidden dir; plus a nested level."""
    root = tmp_path / "level"
    (root / "Beta").mkdir(parents=True)
    (root / "alpha").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / "Beta" / "inner").mkdir()
    return root


# ---------------------------------------------------------------------------
# service: what a level contains
# ---------------------------------------------------------------------------


def test_lists_only_directories_sorted_case_insensitively(tree: Path):
    level = svc.list_subdirectories(str(tree))

    assert [entry["name"] for entry in level.entries] == [".hidden", "alpha", "Beta"]
    assert all(entry["path"].startswith(str(tree.resolve())) for entry in level.entries)
    assert level.total == 3
    assert level.truncated is False


def test_entries_carry_absolute_paths_of_the_children(tree: Path):
    level = svc.list_subdirectories(str(tree))

    paths = {entry["name"]: entry["path"] for entry in level.entries}
    assert paths["alpha"] == str(tree.resolve() / "alpha")


def test_navigating_into_a_child_moves_the_parent_pointer(tree: Path):
    parent_level = svc.list_subdirectories(str(tree))
    inner = svc.list_subdirectories(parent_level.entries[1]["path"])  # alpha

    assert inner.path == str(tree.resolve() / "alpha")
    assert inner.parent == str(tree.resolve())
    assert inner.entries == []


def test_the_parent_of_the_filesystem_root_is_none():
    level = svc.list_subdirectories("/")

    assert level.path == "/"
    assert level.parent is None


def test_empty_path_starts_at_the_server_users_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(svc.Path, "home", classmethod(lambda cls: tmp_path))

    level = svc.list_subdirectories(None)
    assert level.path == str(tmp_path.resolve())

    assert svc.list_subdirectories("").path == str(tmp_path.resolve())


def test_a_tilde_path_is_expanded(tmp_path: Path, monkeypatch):
    # expanduser() reads $HOME on POSIX (not Path.home()), so the env is the knob.
    monkeypatch.setenv("HOME", str(tmp_path))

    level = svc.list_subdirectories("~")
    assert level.path == str(tmp_path.resolve())


def test_the_entry_cap_truncates_and_reports_the_total(tree: Path, monkeypatch):
    monkeypatch.setitem(FILE_BROWSER, "max_entries_per_level", 2)

    level = svc.list_subdirectories(str(tree))

    assert len(level.entries) == 2
    assert level.truncated is True
    assert level.total == 3


# ---------------------------------------------------------------------------
# service: refusals
# ---------------------------------------------------------------------------


def test_a_relative_path_is_refused():
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.list_subdirectories("relative/dir")
    assert excinfo.value.status == 400
    assert "absolute" in excinfo.value.reason


def test_a_missing_directory_is_a_404(tmp_path: Path):
    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.list_subdirectories(str(tmp_path / "gone"))
    assert excinfo.value.status == 404


def test_a_file_is_not_a_directory(tmp_path: Path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(svc.FileBrowserError) as excinfo:
        svc.list_subdirectories(str(target))
    assert excinfo.value.status == 400
    assert "not a directory" in excinfo.value.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_unreadable_directory_is_refused_not_empty(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(svc.FileBrowserError) as excinfo:
            svc.list_subdirectories(str(locked))
        assert "permission denied" in excinfo.value.reason
    finally:
        locked.chmod(0o755)


def test_a_symlinked_directory_is_listed(tmp_path: Path):
    real = tmp_path / "real"
    (real / "child").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    level = svc.list_subdirectories(str(tmp_path))

    assert [entry["name"] for entry in level.entries] == ["link", "real"]
    linked = svc.list_subdirectories(str(link))
    # resolve() reports the real location, so the UI navigates the target.
    assert linked.path == str(real.resolve())
    assert [entry["name"] for entry in linked.entries] == ["child"]


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


def test_route_answers_the_level_payload(tree: Path):
    response = _call(api.system_dirs_handler, _FakeRequest({"path": str(tree)}))
    payload = _payload(response)

    assert payload["success"] is True
    assert payload["path"] == str(tree.resolve())
    assert payload["parent"] == str(tree.resolve().parent)
    assert [entry["name"] for entry in payload["entries"]] == [".hidden", "alpha", "Beta"]
    assert payload["truncated"] is False
    assert payload["total"] == 3


def test_route_decodes_percent_encoded_paths(tmp_path: Path):
    spaced = tmp_path / "My Project"
    spaced.mkdir()

    from urllib.parse import quote

    response = _call(api.system_dirs_handler, _FakeRequest({"path": quote(str(spaced))}))
    payload = _payload(response)

    assert payload["success"] is True
    assert payload["path"] == str(spaced.resolve())


def test_route_maps_a_missing_directory_to_404(tmp_path: Path):
    response = _call(api.system_dirs_handler, _FakeRequest({"path": str(tmp_path / "gone")}))

    assert response.status_code == 404
    assert "does not exist" in _payload(response)["message"]


def test_route_maps_a_relative_path_to_400():
    response = _call(api.system_dirs_handler, _FakeRequest({"path": "somewhere"}))

    assert response.status_code == 400
    assert "absolute" in _payload(response)["message"]
