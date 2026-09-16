"""P2-1 tests: search results are containment-filtered against the search root.

Covers:
- file search skips file symlinks that resolve outside the searched root
- content search skips the same escaped symlinks
- regular files keep appearing in both search modes
"""

import json
import os

import pytest

from agent.tools.file_tools.search_files import build_search_files_tool
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-containment"


def _require_symlink_support(link: str, target: str) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("os.symlink not supported on this platform")
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("cannot create symlink on this platform")


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


@pytest.fixture
def escaped_link(virtual_root):
    """An outside file plus a symlink named escape.txt inside the search root."""
    outside = virtual_root.parent / "outside-secret.txt"
    outside.write_text("root:x:0:0\nsecret-token\n", encoding="utf-8")
    _require_symlink_support(str(virtual_root / "escape.txt"), str(outside))
    return outside


class TestSearchContainment:
    def test_file_search_skips_symlink_escape(self, virtual_root, escaped_link):
        (virtual_root / "keep.txt").write_text("kept\n", encoding="utf-8")

        out = build_search_files_tool()._core("*", target="files", path=".", session_id=SESSION)
        result = json.loads(out)

        assert "/keep.txt" in result["files"]
        assert "/escape.txt" not in result["files"]
        assert str(escaped_link) not in out

    def test_content_search_skips_symlink_escape(self, virtual_root, escaped_link):
        (virtual_root / "keep.txt").write_text("secret-token\n", encoding="utf-8")

        out = build_search_files_tool()._core(
            "secret-token", target="content", path=".", session_id=SESSION
        )
        result = json.loads(out)

        assert [m["path"] for m in result["matches"]] == ["/keep.txt"]

    def test_content_search_skips_link_only_content(self, virtual_root, escaped_link):
        (virtual_root / "keep.txt").write_text("kept\n", encoding="utf-8")

        out = build_search_files_tool()._core(
            "root:x", target="content", path=".", session_id=SESSION
        )

        assert json.loads(out)["matches"] == []

    def test_content_search_keeps_regular_files(self, virtual_root):
        notes = virtual_root / "notes"
        notes.mkdir()
        (notes / "memo.md").write_text("the needle is here\n", encoding="utf-8")

        out = build_search_files_tool()._core(
            "needle", target="content", path=".", session_id=SESSION
        )
        result = json.loads(out)

        assert [m["path"] for m in result["matches"]] == ["/notes/memo.md"]
