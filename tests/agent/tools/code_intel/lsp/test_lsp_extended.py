"""Integration tests for the four Phase 2X LSP tools (hermetic fake server).

Covers ``lsp_rename`` (preview + apply + path gate), ``lsp_diagnostics``
(aggregation, severity, timeout window), ``lsp_format`` (supported / unsupported
/ write), and ``lsp_status`` (honest per-language availability).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp import build_lsp_tools, fallback, resolver
from agent.tools.code_intel.lsp import tools as lsp_tools
from config.features import LSP

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _tools() -> dict:
    return {tool.name: tool for tool in build_lsp_tools("sess")}


@pytest.fixture(autouse=True)
def _fixture_root(monkeypatch: pytest.MonkeyPatch, fixture_repo: Path) -> Path:
    monkeypatch.setenv("SHERRY_LSP_ROOT", str(fixture_repo))
    return fixture_repo


class TestRename:
    def test_preview_does_not_write(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        target = fixture_repo / "m.py"
        before = target.read_text(encoding="utf-8")
        payload = json.loads(_tools()["lsp_rename"]._run(str(target), 1, 5, "renamed"))
        assert payload["applied"] is False
        assert payload["count"] == 1
        assert payload["edit_count"] == 2
        assert target.read_text(encoding="utf-8") == before

    def test_apply_writes_the_edits(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        target = fixture_repo / "m.py"
        payload = json.loads(_tools()["lsp_rename"]._run(str(target), 1, 5, "renamed", False))
        assert payload["applied"] is True
        assert payload["applied_edits"] == 2
        assert str(target) in payload["files_written"]
        content = target.read_text(encoding="utf-8")
        assert "renamed" in content
        assert "helper" not in content

    def test_empty_new_name_is_rejected(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(_tools()["lsp_rename"]._run(str(fixture_repo / "m.py"), 1, 5, "  "))
        assert "new_name" in payload["error"]

    def test_not_installed_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, fixture_repo: Path
    ) -> None:
        missing = "/nonexistent-lsp-bin/fakelsp"
        monkeypatch.setitem(LSP, "lsp_python_server", missing)
        monkeypatch.setitem(
            LSP["lsp_supported_servers"],
            "python",
            {
                "command": [missing, "--stdio"],
                "extensions": [".py"],
                "local_install": None,
                "language_id": "python",
            },
        )
        resolver._clear_cache_for_tests()
        payload = json.loads(_tools()["lsp_rename"]._run(str(fixture_repo / "m.py"), 1, 5, "x"))
        assert payload["available"] is False
        assert "explore" in payload["error"]

    def test_apply_skips_paths_outside_root(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        result = lsp_tools._apply_workspace_edit(
            [
                {
                    "path": str(outside),
                    "edits": [
                        {
                            "range": {
                                "start_line": 1,
                                "start_character": 1,
                                "end_line": 1,
                                "end_character": 1,
                            },
                            "new_text": "y",
                        }
                    ],
                }
            ]
        )
        assert result["applied_edits"] == 0
        assert result["skipped"][0]["path"] == str(outside)
        assert outside.read_text(encoding="utf-8") == "x = 1\n"


class TestDiagnostics:
    def test_diagnostics_are_aggregated(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(_tools()["lsp_diagnostics"]._run(str(fixture_repo / "m.py")))
        assert payload["count"] == 1
        assert payload["timed_out"] is False
        assert payload["diagnostics"][0]["severity"] == "error"
        assert payload["diagnostics"][0]["message"] == "fake diagnostic"
        assert payload["summary"]["error"] == 1

    def test_timeout_window_reports_honestly(
        self, point_to_fake, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        point_to_fake("python")
        monkeypatch.setitem(LSP, "lsp_diagnostics_timeout_s", 0.5)
        monkeypatch.setenv("FAKE_LSP_MODE", "nodiag")
        payload = json.loads(_tools()["lsp_diagnostics"]._run(str(fixture_repo / "m.py")))
        assert payload["count"] == 0
        assert payload["timed_out"] is True


class TestFormat:
    def test_preview_returns_edits_without_writing(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        target = fixture_repo / "m.py"
        before = target.read_text(encoding="utf-8")
        payload = json.loads(_tools()["lsp_format"]._run(str(target)))
        assert payload["supported"] is True
        assert payload["applied"] is False
        assert payload["count"] == 1
        assert target.read_text(encoding="utf-8") == before

    def test_write_applies_edits(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        target = fixture_repo / "m.py"
        payload = json.loads(_tools()["lsp_format"]._run(str(target), write=True))
        assert payload["applied"] is True
        assert payload["applied_edits"] == 1
        assert target.read_text(encoding="utf-8").startswith("# fmt\n")

    def test_unsupported_is_not_faked(
        self, point_to_fake, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        point_to_fake("python")
        monkeypatch.setenv("FAKE_LSP_MODE", "noformat")
        payload = json.loads(_tools()["lsp_format"]._run(str(fixture_repo / "m.py")))
        assert payload["supported"] is False
        assert payload["applied"] is False
        assert "does not support formatting" in payload["message"]

    def test_range_formatting_method(self, point_to_fake, fixture_repo: Path) -> None:
        point_to_fake("python")
        payload = json.loads(_tools()["lsp_format"]._run(str(fixture_repo / "m.py"), 1, 1, 3, 1))
        assert payload["method"] == "textDocument/rangeFormatting"


class TestStatus:
    def test_reports_every_language_honestly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fallback, "resolve_lsp_server", lambda language, cwd=None: "/bin/true")
        payload = json.loads(_tools()["lsp_status"]._run())
        assert payload["count"] == len(LSP["lsp_supported_servers"])
        by_language = {server["language"]: server for server in payload["servers"]}
        assert by_language["python"]["status"] == "available"
        assert by_language["python"]["binary"] == "/bin/true"
        assert payload["summary"]["available"] == payload["count"]
        assert payload["active_servers"] == []

    def test_missing_binaries_are_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fallback, "resolve_lsp_server", lambda language, cwd=None: None)
        payload = json.loads(_tools()["lsp_status"]._run())
        by_language = {server["language"]: server for server in payload["servers"]}
        for language in ("cpp", "java", "ruby", "bash", "vue", "yaml"):
            assert by_language[language]["status"] == "not_installed"
            assert by_language[language]["install_hint"]

    def test_disabled_language_is_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            LSP,
            "lsp_enabled_languages",
            [language for language in LSP["lsp_enabled_languages"] if language != "cpp"],
        )
        monkeypatch.setattr(fallback, "resolve_lsp_server", lambda language, cwd=None: "/bin/true")
        payload = json.loads(_tools()["lsp_status"]._run())
        by_language = {server["language"]: server for server in payload["servers"]}
        assert by_language["cpp"]["status"] == "not_configured"
        assert by_language["python"]["status"] == "available"

    def test_status_starts_no_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent.tools.code_intel.lsp.manager import get_manager

        payload = json.loads(_tools()["lsp_status"]._run())
        assert payload["active_servers"] == []
        assert get_manager().active_count() == 0
