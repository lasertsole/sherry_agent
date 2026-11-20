"""The write-time LSP format hook: only a LIVE server, never a cold start.

``lsp_sync.format_edited_file`` asks the ALREADY-ACQUIRED language server for
``textDocument/formatting`` and applies the edits to the file. Pinned:

* no server for the language (the LSP layer stays lazy) → 0, file untouched;
* an oversized file, an error response, a non-list result, or any transport
  failure → 0, file untouched, never an exception;
* a live client returning edits → the edits land on disk and the count returns;
* ``write_file`` runs the hook for non-``.py`` targets in the OVERWRITE path
  only, and takes the write-license revision AFTER the pass (the license must
  describe the formatted bytes).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import agent.tools.pub_base.lsp_sync as lsp_sync
import agent.tools.pub_base.path_utils as path_utils
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import read_state

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-lsp-write-format"

_EDITS = [
    {
        "range": {"start_line": 1, "start_character": 1, "end_line": 1, "end_character": 20},
        "new_text": "formatted line\n",
    },
]


class _FakeClient:
    def __init__(self, response: dict[str, Any] | None = None, *, fail: bool = False):
        self.response = response if response is not None else {"result": _EDITS}
        self.fail = fail
        self.synced: list[str] = []
        self.requested: list[tuple[str, dict]] = []

    def sync_document(self, file_path: str, content: str) -> bool:
        self.synced.append(file_path)
        return True

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        if self.fail:
            raise OSError("dead pipe")
        self.requested.append((method, params))
        return self.response


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    read_state.forget_all()
    yield root
    read_state.forget_all()


def _patch_client(monkeypatch, client: _FakeClient | None):
    monkeypatch.setattr(lsp_sync, "_client_for", lambda file_path, session_id: ("ts", client))


def _write(session: str, path: str, text: str, **kwargs) -> str:
    return build_write_file_tool()._core(path, text, session_id=session, **kwargs)


class TestFormatEditedFile:
    def test_no_server_leaves_the_file_alone(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("const x = 1\n", encoding="utf-8")
        _patch_client(monkeypatch, None)
        assert lsp_sync.format_edited_file(str(target), SESSION) == 0
        assert target.read_text(encoding="utf-8") == "const x = 1\n"

    def test_live_client_edits_land_on_disk(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("const x   =    1\n", encoding="utf-8")
        _patch_client(monkeypatch, _FakeClient())
        applied = lsp_sync.format_edited_file(str(target), SESSION)
        assert applied == 1
        assert target.read_text(encoding="utf-8") == "formatted line\n"

    def test_an_error_response_is_a_no_op(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("const x = 1\n", encoding="utf-8")
        _patch_client(monkeypatch, _FakeClient({"error": {"message": "unsupported"}}))
        assert lsp_sync.format_edited_file(str(target), SESSION) == 0
        assert target.read_text(encoding="utf-8") == "const x = 1\n"

    def test_a_transport_failure_is_a_no_op(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("const x = 1\n", encoding="utf-8")
        _patch_client(monkeypatch, _FakeClient(fail=True))
        assert lsp_sync.format_edited_file(str(target), SESSION) == 0
        assert target.read_text(encoding="utf-8") == "const x = 1\n"

    def test_an_oversized_file_is_skipped(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("x" * 10, encoding="utf-8")
        _patch_client(monkeypatch, _FakeClient())
        monkeypatch.setitem(lsp_sync.LSP, "lsp_max_file_bytes", 5)
        assert lsp_sync.format_edited_file(str(target), SESSION) == 0
        assert target.read_text(encoding="utf-8") == "x" * 10

    def test_a_non_list_result_is_a_no_op(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("const x = 1\n", encoding="utf-8")
        _patch_client(monkeypatch, _FakeClient({"result": None}))
        assert lsp_sync.format_edited_file(str(target), SESSION) == 0


class TestWriteFileHook:
    def test_non_python_overwrite_runs_the_hook(self, project, monkeypatch):
        target = project / "app.ts"
        target.write_text("old\n", encoding="utf-8")
        from agent.tools.file_tools.read_file import build_read_file_tool

        build_read_file_tool()._core("app.ts", session_id=SESSION)

        calls: list[tuple[str, str]] = []

        def fake_format(file_path: str, session_id: str = "") -> int:
            calls.append((file_path, session_id))
            return 0

        monkeypatch.setattr("agent.tools.pub_base.lsp_sync.format_edited_file", fake_format)
        result = json.loads(_write(SESSION, "app.ts", "new content\n"))
        assert result["success"] is True
        assert calls == [(str(target), SESSION)]

    def test_python_writes_do_not_call_the_hook(self, project, monkeypatch):
        target = project / "script.py"
        target.write_text("x = 1\n", encoding="utf-8")
        from agent.tools.file_tools.read_file import build_read_file_tool

        build_read_file_tool()._core("script.py", session_id=SESSION)

        calls: list[str] = []
        monkeypatch.setattr(
            "agent.tools.pub_base.lsp_sync.format_edited_file",
            lambda file_path, session_id="": calls.append(file_path) or 0,
        )
        result = json.loads(_write(SESSION, "script.py", "y = 2\n"))
        assert result["success"] is True
        assert calls == []

    def test_the_license_covers_the_formatted_bytes(self, project, monkeypatch):
        """The hook rewrites the file BEFORE the revision note is taken."""
        target = project / "app.ts"
        target.write_text("old\n", encoding="utf-8")
        from agent.tools.file_tools.read_file import build_read_file_tool

        build_read_file_tool()._core("app.ts", session_id=SESSION)

        def fake_format(file_path: str, session_id: str = "") -> int:
            # Simulate the server's edits landing on disk during the hook.
            target.write_text("FORMATTED\n", encoding="utf-8")
            return 1

        monkeypatch.setattr("agent.tools.pub_base.lsp_sync.format_edited_file", fake_format)
        result = json.loads(_write(SESSION, "app.ts", "new\n"))
        assert result["success"] is True
        assert target.read_text(encoding="utf-8") == "FORMATTED\n"
        # The license (taken after the pass) must accept an overwrite of the
        # formatted bytes — a license on the pre-format revision would refuse.
        second = json.loads(_write(SESSION, "app.ts", "again\n"))
        assert second["success"] is True, second
