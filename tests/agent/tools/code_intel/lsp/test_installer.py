"""Unit tests for the LSP auto-installer (allow-listed, fail-open)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agent.tools.code_intel.lsp import installer
from config.features import LSP

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _completed(returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


class TestInstallGuards:
    def test_auto_install_disabled_returns_hint(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", False)
        result = installer.install_lsp_server("python")
        assert result["ok"] is False
        assert "Auto-install disabled" in result["message"]
        assert "basedpyright" in result["message"]

    def test_missing_command_returns_hint(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)
        result = installer.install_lsp_server("rust")
        assert result["ok"] is False
        assert "No auto-install command" in result["message"]


class TestInstallExecution:
    def test_success_reports_binary(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)
        monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _completed(0))
        monkeypatch.setattr(installer, "resolve_lsp_server", lambda language, cwd: "/fake/bin/ls")
        result = installer.install_lsp_server("python")
        assert result["ok"] is True
        assert result["binary_path"] == "/fake/bin/ls"

    def test_success_but_binary_missing(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)
        monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _completed(0))
        monkeypatch.setattr(installer, "resolve_lsp_server", lambda language, cwd: None)
        result = installer.install_lsp_server("python")
        assert result["ok"] is False
        assert "not found" in result["message"]

    def test_timeout_is_reported(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)

        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="pip", timeout=60)

        monkeypatch.setattr(installer.subprocess, "run", _timeout)
        result = installer.install_lsp_server("python")
        assert result["ok"] is False
        assert "timed out" in result["message"]

    def test_missing_tool_is_reported(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)

        def _missing(*a, **k):
            raise FileNotFoundError("pip")

        monkeypatch.setattr(installer.subprocess, "run", _missing)
        result = installer.install_lsp_server("python")
        assert result["ok"] is False
        assert "not found" in result["message"]

    def test_nonzero_exit_is_reported(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)
        monkeypatch.setattr(
            installer.subprocess, "run", lambda *a, **k: _completed(2, stderr="boom")
        )
        result = installer.install_lsp_server("python")
        assert result["ok"] is False
        assert "exit 2" in result["message"]


class TestEnvScrubbing:
    def test_install_runs_with_scrubbed_env(self, monkeypatch) -> None:
        monkeypatch.setitem(LSP, "lsp_auto_install", True)
        captured: dict = {}

        def _run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env")
            return _completed(0)

        monkeypatch.setattr(installer.subprocess, "run", _run)
        monkeypatch.setattr(installer, "resolve_lsp_server", lambda language, cwd: "/x")
        monkeypatch.setenv("MAIN_LLM_API_KEY", "super-secret")
        installer.install_lsp_server("python")
        assert captured["command"] == LSP["lsp_auto_install_commands"]["python"]
        assert "MAIN_LLM_API_KEY" not in captured["env"]
