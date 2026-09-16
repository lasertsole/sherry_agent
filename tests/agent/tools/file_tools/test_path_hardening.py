"""P0-3 hardening tests: symlink refusal + TOCTOU closure in the file tools.

Covers:
- symlink final component pointing outside ROOT_DIR is refused by read/write/patch
- a symlink swapped in between resolve() and open() (TOCTOU) is refused
- regular files keep working through the same tools (write/read/patch round trip,
  append, parent-directory creation)
"""

import json
import os

import pytest

from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-hardening"


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


def _link(link_path, target) -> None:
    _require_symlink_support(str(link_path), str(target))


class TestSymlinkRejection:
    def test_read_rejects_symlink_to_external_target(self, virtual_root):
        _link(virtual_root / "link.txt", "/etc/passwd")

        result = json.loads(build_read_file_tool()._core("link.txt", session_id=SESSION))

        assert "error" in result
        assert "root:" not in result.get("content", "")

    def test_write_rejects_symlink_to_external_target(self, virtual_root):
        outside = virtual_root.parent / "outside-write-target.txt"
        outside.write_text("original", encoding="utf-8")
        _link(virtual_root / "link.txt", outside)

        result = json.loads(build_write_file_tool()._core("link.txt", "pwned", session_id=SESSION))

        assert "error" in result
        assert outside.read_text(encoding="utf-8") == "original"

    def test_patch_rejects_symlink_to_external_target(self, virtual_root):
        outside = virtual_root.parent / "outside-patch-target.txt"
        outside.write_text("original", encoding="utf-8")
        _link(virtual_root / "link.txt", outside)

        result = json.loads(
            build_patch_file_tool()._core(
                "link.txt",
                old_string="original",
                new_string="pwned",
                session_id=SESSION,
            )
        )

        assert "error" in result
        assert outside.read_text(encoding="utf-8") == "original"


class TestToctouRace:
    def test_read_rejects_link_swapped_after_resolve(self, virtual_root, monkeypatch):
        victim = virtual_root / "victim.txt"
        victim.write_text("safe", encoding="utf-8")
        secret = virtual_root.parent / "toctou-secret.txt"
        secret.write_text("top-secret", encoding="utf-8")

        import agent.tools.file_tools.read_file as read_file_module

        real_resolve = read_file_module.resolve_project_path

        def racing_resolve(file_path):
            resolved = real_resolve(file_path)
            victim.unlink()
            _link(victim, secret)
            return resolved

        monkeypatch.setattr(read_file_module, "resolve_project_path", racing_resolve)

        result = json.loads(build_read_file_tool()._core("victim.txt", session_id=SESSION))

        assert "error" in result
        assert "top-secret" not in result.get("content", "")

    def test_write_rejects_link_swapped_after_resolve(self, virtual_root, monkeypatch):
        victim = virtual_root / "victim.txt"
        victim.write_text("safe", encoding="utf-8")
        secret = virtual_root.parent / "toctou-secret.txt"
        secret.write_text("top-secret", encoding="utf-8")

        import agent.tools.file_tools.write_file as write_file_module

        real_resolve = write_file_module.resolve_project_path

        def racing_resolve(file_path):
            resolved = real_resolve(file_path)
            victim.unlink()
            _link(victim, secret)
            return resolved

        monkeypatch.setattr(write_file_module, "resolve_project_path", racing_resolve)

        out = build_write_file_tool()._core("victim.txt", "pwned", session_id=SESSION)

        assert out.startswith("Error:")
        assert secret.read_text(encoding="utf-8") == "top-secret"


class TestRegularFilesUnaffected:
    def test_write_read_patch_roundtrip(self, virtual_root):
        write_tool = build_write_file_tool()
        read_tool = build_read_file_tool()
        patch_tool = build_patch_file_tool()

        out = write_tool._core("notes/plain.txt", "hello\nworld\n", session_id=SESSION)
        assert "successfully" in out
        written = virtual_root / "notes" / "plain.txt"
        assert written.read_text(encoding="utf-8") == "hello\nworld\n"

        read_result = json.loads(read_tool._core("notes/plain.txt", session_id=SESSION))
        assert "hello" in read_result["content"]
        assert read_result["total_lines"] == 2

        patch_result = json.loads(
            patch_tool._core(
                "notes/plain.txt",
                old_string="world",
                new_string="there",
                session_id=SESSION,
            )
        )
        assert patch_result["success"] is True
        assert written.read_text(encoding="utf-8") == "hello\nthere\n"

    def test_write_append_preserves_existing_content(self, virtual_root):
        target = virtual_root / "log.txt"
        target.write_text("line1\n", encoding="utf-8")

        out = build_write_file_tool()._core("log.txt", "line2\n", append=True, session_id=SESSION)

        assert "successfully" in out
        assert target.read_text(encoding="utf-8") == "line1\nline2\n"

    def test_write_failure_returns_error_string(self, virtual_root):
        target = virtual_root / "locked"
        target.mkdir()

        out = build_write_file_tool()._core("locked", "data", session_id=SESSION)

        assert out.startswith("Error:")
