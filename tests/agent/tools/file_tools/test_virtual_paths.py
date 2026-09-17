"""Virtual-path tests: file tools render model-visible paths without leaking ROOT_DIR.

Covers:
- read_file errors use virtual paths / safe error details
- write_file success messages use virtual paths
- patch_file diff headers and JSON paths use virtual paths
- search results use virtual paths
"""

import errno
import json
import os

import pytest

from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.search_files import build_search_files_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-virtual"


@pytest.fixture
def virtual_root(tmp_path, monkeypatch):
    """Point ROOT_DIR at tmp_path; fail external fallback closed as a subagent."""
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)

    from runtime import state_register_mem
    import runtime

    class _FakeDB:
        def __init__(self):
            self.store: dict[tuple[str, str], object] = {}

        def get_state(self, session_id, key, default=None):
            return self.store.get((session_id, key), default)

        def set_state(self, session_id, key, value):
            self.store[(session_id, key)] = value
            return True

    monkeypatch.setattr(state_register_mem, "_states", {})
    monkeypatch.setattr(runtime, "state_register_db", _FakeDB())
    state_register_mem.set_state(SESSION, "caller_scope", "subagent")
    return root


class TestReadFileVirtualPaths:
    def test_missing_file_error_uses_virtual_path(self, virtual_root):
        result = json.loads(build_read_file_tool()._core("missing.txt", session_id=SESSION))

        assert result["error"] == "File not found: /missing.txt"
        assert str(virtual_root) not in result["error"]

    def test_read_error_does_not_leak_root_dir(self, virtual_root, monkeypatch):
        secret = virtual_root / "secret.txt"
        secret.write_text("data", encoding="utf-8")

        import agent.tools.file_tools.read_file as read_file_module

        def _denied(path, flags, mode=0o644):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(secret))

        monkeypatch.setattr(read_file_module, "_open_no_follow", _denied)

        result = json.loads(build_read_file_tool()._core("secret.txt", session_id=SESSION))

        assert result["error"] == "Failed to read file: PermissionError: Permission denied"
        assert str(virtual_root) not in json.dumps(result)


class TestWriteFileVirtualPaths:
    def test_success_message_uses_virtual_path(self, virtual_root):
        out = build_write_file_tool()._core("notes/new.txt", "hi", session_id=SESSION)

        assert out == "File written successfully to /notes/new.txt."
        assert str(virtual_root) not in out


class TestPatchFileVirtualPaths:
    def test_diff_header_and_path_use_virtual_path(self, virtual_root):
        target = virtual_root / "notes" / "note.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("status: old\n", encoding="utf-8")

        result = json.loads(
            build_patch_file_tool()._core(
                "notes/note.txt",
                old_string="status: old",
                new_string="status: new",
                session_id=SESSION,
            )
        )

        assert result["success"] is True
        assert result["path"] == "/notes/note.txt"
        assert "/notes/note.txt" in result["diff"]
        assert str(virtual_root) not in json.dumps(result)

    def test_no_match_reports_virtual_path(self, virtual_root):
        target = virtual_root / "notes" / "note.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hello\n", encoding="utf-8")

        result = json.loads(
            build_patch_file_tool()._core(
                "notes/note.txt",
                old_string="not-present-anywhere",
                new_string="x",
                session_id=SESSION,
            )
        )

        assert result["path"] == "/notes/note.txt"
        assert str(virtual_root) not in json.dumps(result)


class TestSearchFilesVirtualPaths:
    def test_file_search_results_use_virtual_paths(self, virtual_root):
        src = virtual_root / "src"
        src.mkdir()
        (src / "app.py").write_text("x = 1\n", encoding="utf-8")
        (src / "util.py").write_text("y = 2\n", encoding="utf-8")

        result = json.loads(
            build_search_files_tool()._core("*.py", target="files", path="src", session_id=SESSION)
        )

        assert sorted(result["files"]) == ["/src/app.py", "/src/util.py"]
        assert str(virtual_root) not in json.dumps(result)

    def test_content_search_results_use_virtual_paths(self, virtual_root):
        src = virtual_root / "src"
        src.mkdir()
        (src / "app.py").write_text("needle = 1\n", encoding="utf-8")

        result = json.loads(
            build_search_files_tool()._core(
                "needle", target="content", path="src", session_id=SESSION
            )
        )

        assert [m["path"] for m in result["matches"]] == ["/src/app.py"]
        assert str(virtual_root) not in json.dumps(result)
