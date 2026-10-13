"""Unit tests for the LSP availability check and fallback messages."""

from __future__ import annotations

import pytest

from agent.tools.code_intel.lsp import fallback

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class TestCheckAvailability:
    def test_not_configured_for_disabled_language(self) -> None:
        status, message = fallback.check_lsp_availability("haskell")
        assert status == "not_configured"
        assert "haskell" in message

    def test_not_installed_returns_hint_and_local_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(fallback, "resolve_lsp_server", lambda language, cwd: None)
        status, message = fallback.check_lsp_availability("python")
        assert status == "not_installed"
        assert "pip install basedpyright" in message
        assert "uv add --dev basedpyright" in message

    def test_available_returns_binary_path(self, monkeypatch) -> None:
        monkeypatch.setattr(fallback, "resolve_lsp_server", lambda language, cwd: "/bin/ls")
        assert fallback.check_lsp_availability("python") == ("available", "/bin/ls")


class TestFallbackMessage:
    def test_not_configured_mentions_fallbacks(self) -> None:
        message = fallback.build_fallback_message(
            "haskell", "lsp_goto_definition", "not_configured", "nope"
        )
        assert "lsp_goto_definition" in message
        assert "explore" in message and "terminal" in message

    def test_not_installed_mentions_install_and_fallbacks(self) -> None:
        message = fallback.build_fallback_message(
            "rust", "lsp_find_references", "not_installed", "rustup component add rust-analyzer"
        )
        assert "rust" in message
        assert "rustup component add rust-analyzer" in message
        assert "explore" in message and "terminal" in message

    def test_available_is_empty(self) -> None:
        assert (
            fallback.build_fallback_message("python", "lsp_goto_definition", "available", "/bin/ls")
            == ""
        )
