"""Integration tests for the four RESEARCHER-only LSP tools (hermetic fake server)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp import build_lsp_tools
from config.features import LSP

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _tools() -> dict:
    return {tool.name: tool for tool in build_lsp_tools("sess")}


@pytest.fixture(autouse=True)
def _fixture_root(monkeypatch: pytest.MonkeyPatch, fixture_repo: Path) -> Path:
    monkeypatch.setenv("SHERRY_LSP_ROOT", str(fixture_repo))
    return fixture_repo


class TestHappyPath:
    def test_goto_definition(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(
            _tools()["lsp_goto_definition"]._run(str(fixture_repo / "m.py"), 5, 12)
        )
        assert payload["count"] == 1
        assert payload["definitions"][0]["range"]["start_line"] == 1

    def test_find_references(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(_tools()["lsp_find_references"]._run(str(fixture_repo / "m.py"), 1, 5))
        assert payload["count"] == 2

    def test_workspace_symbol(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(
            _tools()["lsp_workspace_symbol"]._run("fake", file_path=str(fixture_repo / "m.py"))
        )
        assert payload["count"] == 1
        assert payload["symbols"][0]["name"] == "fakesym"

    def test_call_hierarchy_incoming(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(
            _tools()["lsp_call_hierarchy"]._run(str(fixture_repo / "m.py"), 1, 5, "incoming")
        )
        assert payload["direction"] == "incoming"
        assert payload["calls"][0]["name"] == "callerfn"

    def test_call_hierarchy_outgoing(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(
            _tools()["lsp_call_hierarchy"]._run(str(fixture_repo / "m.py"), 1, 5, "outgoing")
        )
        assert payload["calls"][0]["name"] == "calleefn"


class TestPathSafety:
    def test_traversal_is_rejected(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(_tools()["lsp_goto_definition"]._run("../escape.py", 1, 1))
        assert "traversal" in payload["error"]

    def test_out_of_root_is_rejected(self, point_to_fake, tmp_path: Path) -> None:
        point_to_fake("python")
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        payload = json.loads(_tools()["lsp_goto_definition"]._run(str(outside), 1, 1))
        assert "escapes" in payload["error"]

    def test_unsupported_extension_is_reported(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        notes = fixture_repo / "notes.txt"
        notes.write_text("hello\n", encoding="utf-8")
        payload = json.loads(_tools()["lsp_goto_definition"]._run(str(notes), 1, 1))
        assert "language" in payload["error"]


class TestFailOpen:
    def test_not_installed_returns_hint(
        self, monkeypatch: pytest.MonkeyPatch, fixture_repo: Path
    ) -> None:
        missing = "/nonexistent-lsp-bin/fakelsp"
        monkeypatch.setitem(LSP, "lsp_python_server", missing)
        monkeypatch.setitem(
            LSP["lsp_supported_servers"],
            "python",
            {"command": [missing, "--stdio"], "extensions": [".py"], "local_install": None},
        )
        from agent.tools.code_intel.lsp import resolver

        resolver._clear_cache_for_tests()
        payload = json.loads(
            _tools()["lsp_goto_definition"]._run(str(fixture_repo / "m.py"), 5, 12)
        )
        assert payload["available"] is False
        assert "explore" in payload["error"] and "search_files" in payload["error"]

    def test_invalid_direction_is_reported(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(
            _tools()["lsp_call_hierarchy"]._run(str(fixture_repo / "m.py"), 1, 5, "sideways")
        )
        assert "direction" in payload["error"]

    def test_server_error_is_surfaced(
        self, point_to_fake, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        point_to_fake("python")
        monkeypatch.setenv("FAKE_LSP_MODE", "error")
        payload = json.loads(
            _tools()["lsp_goto_definition"]._run(str(fixture_repo / "m.py"), 5, 12)
        )
        assert payload["error"] == "fake error"

    def test_request_timeout_is_surfaced(
        self, point_to_fake, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        point_to_fake("python")
        monkeypatch.setitem(LSP, "lsp_request_timeout_s", 0.5)
        monkeypatch.setenv("FAKE_LSP_MODE", "slow")
        payload = json.loads(
            _tools()["lsp_goto_definition"]._run(str(fixture_repo / "m.py"), 5, 12)
        )
        assert "timed out" in payload["error"]


class TestToolSurface:
    def test_builder_returns_four_researcher_tools(self) -> None:
        tools = build_lsp_tools("sess")
        assert [tool.name for tool in tools] == [
            "lsp_goto_definition",
            "lsp_find_references",
            "lsp_workspace_symbol",
            "lsp_call_hierarchy",
        ]
        assert all(tool.metadata["scope"] == "researcher_only" for tool in tools)
