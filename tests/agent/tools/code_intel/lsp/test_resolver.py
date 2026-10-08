"""Unit tests for the LSP multi-tier binary resolver."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp import resolver
from config.features import LSP

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho lsp\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _neutralize(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable every tier except the explicit one."""
    monkeypatch.setattr(resolver, "_resolve_local", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "runtime_dir", lambda: Path("/nonexistent-lsp-runtime"))
    monkeypatch.setattr(resolver, "_resolve_from_path", lambda *a, **k: None)
    monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [])


class TestExplicitTier:
    def test_absolute_command_is_returned(self, tmp_path: Path, monkeypatch) -> None:
        _neutralize(monkeypatch)
        binary = _make_executable(tmp_path / "bin" / "langserver")
        monkeypatch.setattr(resolver, "_server_command", lambda language: str(binary))
        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)

    def test_missing_absolute_command_falls_through(self, tmp_path: Path, monkeypatch) -> None:
        _neutralize(monkeypatch)
        monkeypatch.setattr(
            resolver, "_server_command", lambda language: str(tmp_path / "missing-langserver")
        )
        assert resolver.resolve_lsp_server("python", str(tmp_path)) is None


class TestRepoLocalTier:
    def test_marker_gated_venv_bin_is_found(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(resolver, "runtime_dir", lambda: Path("/nonexistent-lsp-runtime"))
        monkeypatch.setattr(resolver, "_resolve_from_path", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [])
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")

        project = tmp_path / "project"
        binary = _make_executable(project / ".venv" / "bin" / "fakelsp")
        (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

        assert resolver.resolve_lsp_server("python", str(project)) == str(binary)

    def test_bin_dir_untrusted_without_marker(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(resolver, "runtime_dir", lambda: Path("/nonexistent-lsp-runtime"))
        monkeypatch.setattr(resolver, "_resolve_from_path", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [])
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")

        project = tmp_path / "project"
        _make_executable(project / ".venv" / "bin" / "fakelsp")
        # no pyproject.toml / requirements / setup / pyrightconfig marker

        assert resolver.resolve_lsp_server("python", str(project)) is None

    def test_node_modules_requires_package_json(self, tmp_path: Path) -> None:
        monkeypatch = pytest.MonkeyPatch()
        try:
            suffixes = resolver._executable_suffixes()
            project = tmp_path / "web"
            binary = _make_executable(project / "node_modules" / ".bin" / "tls")
            # no package.json yet → untrusted
            assert resolver._resolve_local("tls", str(project), suffixes, "typescript") is None
            (project / "package.json").write_text("{}", encoding="utf-8")
            assert resolver._resolve_local("tls", str(project), suffixes, "typescript") == str(
                binary
            )
        finally:
            monkeypatch.undo()


class TestRuntimePathHomebrewTiers:
    def test_runtime_tier(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(resolver, "_resolve_local", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_resolve_from_path", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [])
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")
        monkeypatch.setattr(resolver, "runtime_dir", lambda: tmp_path)
        binary = _make_executable(tmp_path / "fakelsp")

        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)

    def test_path_tier(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(resolver, "_resolve_local", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "runtime_dir", lambda: Path("/nonexistent-lsp-runtime"))
        monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [])
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")
        bindir = tmp_path / "bin"
        binary = _make_executable(bindir / "fakelsp")
        monkeypatch.setenv("PATH", str(bindir))

        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)

    def test_homebrew_tier(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(resolver, "_resolve_local", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "runtime_dir", lambda: Path("/nonexistent-lsp-runtime"))
        monkeypatch.setattr(resolver, "_resolve_from_path", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")
        monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [str(tmp_path)])
        binary = _make_executable(tmp_path / "fakelsp")

        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)

    def test_all_tiers_miss_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        _neutralize(monkeypatch)
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")
        assert resolver.resolve_lsp_server("python", str(tmp_path)) is None

    def test_homebrew_dirs_by_platform(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        assert "/opt/homebrew/bin" in resolver._homebrew_dirs()
        monkeypatch.setattr(sys, "platform", "linux")
        assert "/home/linuxbrew/.linuxbrew/bin" in resolver._homebrew_dirs()
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolver._homebrew_dirs() == []


class TestCaching:
    def test_resolution_is_cached(self, tmp_path: Path, monkeypatch) -> None:
        binary = _make_executable(tmp_path / "fakelsp")
        monkeypatch.setattr(resolver, "_server_command", lambda language: str(binary))
        calls = {"n": 0}
        real = resolver._resolve_uncached

        def _counted(command: str, work_dir: str, language: str) -> str | None:
            calls["n"] += 1
            return real(command, work_dir, language)

        monkeypatch.setattr(resolver, "_resolve_uncached", _counted)
        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)
        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)
        assert calls["n"] == 1

    def test_cache_can_be_disabled(self, tmp_path: Path, monkeypatch) -> None:
        binary = _make_executable(tmp_path / "fakelsp")
        monkeypatch.setattr(resolver, "_server_command", lambda language: str(binary))
        monkeypatch.setitem(LSP, "lsp_resolve_cache_enabled", False)
        calls = {"n": 0}
        real = resolver._resolve_uncached

        def _counted(command: str, work_dir: str, language: str) -> str | None:
            calls["n"] += 1
            return real(command, work_dir, language)

        monkeypatch.setattr(resolver, "_resolve_uncached", _counted)
        resolver.resolve_lsp_server("python", str(tmp_path))
        resolver.resolve_lsp_server("python", str(tmp_path))
        assert calls["n"] == 2

    def test_clear_cache_for_tests(self, tmp_path: Path, monkeypatch) -> None:
        _neutralize(monkeypatch)
        monkeypatch.setattr(resolver, "_server_command", lambda language: "fakelsp")
        assert resolver.resolve_lsp_server("python", str(tmp_path)) is None
        binary = _make_executable(tmp_path / "fakelsp")
        monkeypatch.setattr(resolver, "_resolve_from_path", lambda command, suffixes: str(binary))
        assert resolver.resolve_lsp_server("python", str(tmp_path)) is None  # cached miss
        resolver._clear_cache_for_tests()
        assert resolver.resolve_lsp_server("python", str(tmp_path)) == str(binary)


class TestWindowsSuffixes:
    def test_probe_matches_pathext(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        suffixes = resolver._executable_suffixes()
        assert ".exe" in suffixes and ".cmd" in suffixes
        binary = tmp_path / "fakelsp.cmd"
        binary.write_text("echo lsp\n", encoding="utf-8")
        assert resolver._probe(str(tmp_path), "fakelsp", suffixes) == str(binary)


class TestInstallHints:
    def test_install_hint_python(self) -> None:
        assert "basedpyright" in resolver.get_install_hint("python")

    def test_local_install_hint_python(self) -> None:
        assert resolver.get_local_install_hint("python") == "uv add --dev basedpyright"

    def test_local_install_hint_rust_is_none(self) -> None:
        assert resolver.get_local_install_hint("rust") is None

    def test_unknown_language_hint(self) -> None:
        assert "haskell" in resolver.get_install_hint("haskell")


_NEW_LANGUAGES = ["cpp", "java", "ruby", "bash", "vue", "yaml"]
_NPM_LANGUAGES = ["bash", "vue", "yaml"]


class TestPhase2XLanguages:
    def test_supported_servers_carry_language_id(self) -> None:
        for language, spec in LSP["lsp_supported_servers"].items():
            assert spec["language_id"], language
            assert spec["command"], language
            assert spec["extensions"], language

    @pytest.mark.parametrize("language", _NEW_LANGUAGES)
    def test_new_language_resolves_from_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str
    ) -> None:
        command = LSP["lsp_supported_servers"][language]["command"][0]
        bindir = tmp_path / language
        binary = _make_executable(bindir / command)
        monkeypatch.setattr(resolver, "_resolve_local", lambda *a, **k: None)
        monkeypatch.setattr(resolver, "runtime_dir", lambda: Path("/nonexistent-lsp-runtime"))
        monkeypatch.setattr(resolver, "_homebrew_dirs", lambda: [])
        monkeypatch.setenv("PATH", str(bindir))
        assert resolver.resolve_lsp_server(language, str(tmp_path)) == str(binary)

    @pytest.mark.parametrize("language", _NEW_LANGUAGES)
    def test_new_language_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str
    ) -> None:
        _neutralize(monkeypatch)
        assert resolver.resolve_lsp_server(language, str(tmp_path)) is None

    @pytest.mark.parametrize("language", _NPM_LANGUAGES)
    def test_node_bin_requires_package_json(self, tmp_path: Path, language: str) -> None:
        suffixes = resolver._executable_suffixes()
        command = LSP["lsp_supported_servers"][language]["command"][0]
        project = tmp_path / language
        binary = _make_executable(project / "node_modules" / ".bin" / command)
        assert resolver._resolve_local(command, str(project), suffixes, language) is None
        (project / "package.json").write_text("{}", encoding="utf-8")
        assert resolver._resolve_local(command, str(project), suffixes, language) == str(binary)

    def test_ruby_bin_requires_gemfile(self, tmp_path: Path) -> None:
        suffixes = resolver._executable_suffixes()
        project = tmp_path / "rb"
        binary = _make_executable(project / "bin" / "ruby-lsp")
        assert resolver._resolve_local("ruby-lsp", str(project), suffixes, "ruby") is None
        (project / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
        assert resolver._resolve_local("ruby-lsp", str(project), suffixes, "ruby") == str(binary)

    def test_cpp_and_java_have_no_repo_local_rule(self) -> None:
        rules = LSP["lsp_repo_local_bin_rules"]
        assert "cpp" not in rules and "java" not in rules

    def test_new_language_install_hints(self) -> None:
        assert "clangd.llvm.org" in resolver.get_install_hint("cpp")
        assert "ruby-lsp" in resolver.get_install_hint("ruby")
        assert "bash-language-server" in resolver.get_install_hint("bash")
        assert resolver.get_local_install_hint("ruby") == "bundle add ruby-lsp"
        assert resolver.get_local_install_hint("bash") is not None
        assert resolver.get_local_install_hint("cpp") is None


class TestConfigSurface:
    def test_double_re_export_identical(self) -> None:
        from config.features import LSP as TOP
        from config.features.agent_side import LSP as SIDE

        assert TOP is SIDE

    def test_keys_match_annotations(self) -> None:
        from config.features.agent_side import LspConfig

        assert set(LSP.keys()) == set(LspConfig.__annotations__.keys())

    def test_defaults(self) -> None:
        assert LSP["lsp_request_timeout_s"] == 10.0
        assert LSP["lsp_server_start_timeout_s"] == 15.0
        assert LSP["lsp_auto_install"] is False
        assert set(LSP["lsp_enabled_languages"]) == {
            "python",
            "typescript",
            "rust",
            "go",
            "cpp",
            "java",
            "ruby",
            "bash",
            "vue",
            "yaml",
        }
        assert LSP["lsp_max_concurrent_servers"] >= 1
        assert LSP["lsp_idle_shutdown_s"] > 0
