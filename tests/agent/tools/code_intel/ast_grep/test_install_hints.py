"""Unit tests for ast-grep install hints and the config re-export surface."""

from __future__ import annotations

import pytest

from agent.tools.code_intel.ast_grep.install_hints import (
    sg_binary_not_found_message,
    sg_install_hints,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class TestInstallHints:
    def test_all_platforms_include_sherry_recovery_paths(self) -> None:
        for platform in ("linux", "darwin", "win32"):
            hints = sg_install_hints(platform)
            assert any("SHERRY_SG_PATH" in hint for hint in hints), platform
            assert any("provision" in hint.lower() for hint in hints), platform

    def test_linux_hints(self) -> None:
        hints = sg_install_hints("linux")
        assert "npm install -g @ast-grep/cli" in hints
        assert "cargo install ast-grep --locked" in hints
        assert "pip install ast-grep-cli" in hints

    def test_darwin_hints(self) -> None:
        hints = sg_install_hints("darwin")
        assert "brew install ast-grep" in hints
        assert "cargo install ast-grep --locked" in hints

    def test_win32_hints(self) -> None:
        hints = sg_install_hints("win32")
        assert "scoop install main/ast-grep" in hints
        assert "winget install ast-grep" in hints

    def test_unknown_platform_falls_back_to_linux(self) -> None:
        assert sg_install_hints("freebsd") == sg_install_hints("linux")

    def test_not_found_message_mentions_probe_and_platform(self) -> None:
        message = sg_binary_not_found_message("linux")
        assert "linux" in message
        assert "--version" in message


class TestConfigReExport:
    def test_double_re_export_is_identical(self) -> None:
        from config.features import AST_GREP as AST_GREP_TOP
        from config.features.agent_side import AST_GREP as AST_GREP_SIDE

        assert AST_GREP_TOP is AST_GREP_SIDE

    def test_typed_dict_keys_match_instance(self) -> None:
        from config.features.agent_side import AST_GREP, AstGrepConfig

        assert set(AST_GREP.keys()) == set(AstGrepConfig.__annotations__.keys())

    def test_defaults(self) -> None:
        from config.features import AST_GREP

        assert AST_GREP["ast_grep_strictness_default"] == "smart"
        assert AST_GREP["ast_grep_max_paths"] == 64
        assert AST_GREP["ast_grep_path_env_key"] == "SHERRY_SG_PATH"
        assert "python" in AST_GREP["ast_grep_supported_languages"]
