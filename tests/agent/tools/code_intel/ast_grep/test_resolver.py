"""Unit tests for the 5-tier ast-grep binary resolver."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.tools.code_intel.ast_grep import resolver

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _make_executable(path: Path, output: str = "ast-grep 0.43.0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _clean_resolver(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHERRY_SG_PATH", raising=False)
    monkeypatch.delenv("SHERRY_SG_ARCH", raising=False)
    resolver._clear_cache_for_tests()
    yield
    resolver._clear_cache_for_tests()


def _neutralize_all_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver, "_env_override_candidates", list)
    monkeypatch.setattr(resolver, "_runtime_candidates", list)
    monkeypatch.setattr(resolver, "_skill_bin_candidates", list)
    monkeypatch.setattr(resolver, "_path_candidates", list)
    monkeypatch.setattr(resolver, "_homebrew_candidates", list)


class TestRuntimeSlug:
    def test_linux_aarch64_maps_to_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(resolver._platform, "machine", lambda: "aarch64")
        assert resolver._runtime_slug() == "linux-arm64"

    def test_darwin_x86_64_maps_to_x64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(resolver._platform, "machine", lambda: "x86_64")
        assert resolver._runtime_slug() == "darwin-x64"

    def test_arch_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("SHERRY_SG_ARCH", "arm64")
        assert resolver._runtime_slug() == "win32-arm64"


class TestCandidateExistsAndProbe:
    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        assert resolver._candidate_exists(str(tmp_path / "nope")) is False

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        empty = tmp_path / "sg"
        empty.write_bytes(b"")
        assert resolver._candidate_exists(str(empty)) is False

    def test_probe_accepts_ast_grep_output(self, tmp_path: Path) -> None:
        binary = _make_executable(tmp_path / "sg", "ast-grep 0.43.0")
        assert resolver._probe_version(str(binary)) is True
        assert resolver._accepts(str(binary)) is True

    def test_probe_rejects_unrelated_output(self, tmp_path: Path) -> None:
        binary = _make_executable(tmp_path / "sg", "hello world")
        assert resolver._probe_version(str(binary)) is False
        assert resolver._accepts(str(binary)) is False

    def test_probe_timeout_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slow = tmp_path / "sg"
        slow.write_text("#!/bin/sh\nsleep 5\necho ast-grep\n", encoding="utf-8")
        slow.chmod(0o755)
        monkeypatch.setitem(resolver.AST_GREP, "ast_grep_version_probe_timeout_ms", 200)
        assert resolver._probe_version(str(slow)) is False


class TestTierHitAndMiss:
    def test_env_override_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resolver, "_runtime_candidates", list)
        monkeypatch.setattr(resolver, "_skill_bin_candidates", list)
        monkeypatch.setattr(resolver, "_path_candidates", list)
        monkeypatch.setattr(resolver, "_homebrew_candidates", list)
        binary = _make_executable(tmp_path / "sg")
        monkeypatch.setenv("SHERRY_SG_PATH", str(binary))
        assert resolver.resolve_sg_binary() == str(binary)

    def test_runtime_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resolver, "_env_override_candidates", list)
        monkeypatch.setattr(resolver, "_skill_bin_candidates", list)
        monkeypatch.setattr(resolver, "_path_candidates", list)
        monkeypatch.setattr(resolver, "_homebrew_candidates", list)
        monkeypatch.setattr(resolver, "runtime_dir", lambda: tmp_path)
        binary = _make_executable(tmp_path / "sg")
        assert resolver.resolve_sg_binary() == str(binary)

    def test_skill_bin_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resolver, "_env_override_candidates", list)
        monkeypatch.setattr(resolver, "_runtime_candidates", list)
        monkeypatch.setattr(resolver, "_path_candidates", list)
        monkeypatch.setattr(resolver, "_homebrew_candidates", list)
        monkeypatch.setattr(resolver, "ROOT_DIR", tmp_path)
        binary = _make_executable(tmp_path / "skills" / "ast-grep" / "bin" / "sg")
        assert resolver.resolve_sg_binary() == str(binary)

    def test_path_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resolver, "_env_override_candidates", list)
        monkeypatch.setattr(resolver, "_runtime_candidates", list)
        monkeypatch.setattr(resolver, "_skill_bin_candidates", list)
        monkeypatch.setattr(resolver, "_homebrew_candidates", list)
        bindir = tmp_path / "bin"
        binary = _make_executable(bindir / "ast-grep")
        monkeypatch.setenv("PATH", str(bindir))
        assert resolver.resolve_sg_binary() == str(binary)

    def test_all_tiers_miss_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _neutralize_all_tiers(monkeypatch)
        monkeypatch.setattr(resolver, "runtime_dir", lambda: tmp_path)
        monkeypatch.setenv("PATH", str(tmp_path))
        assert resolver.resolve_sg_binary() is None

    def test_homebrew_candidates_by_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        darwin = resolver._homebrew_candidates()
        assert "/opt/homebrew/bin/ast-grep" in darwin
        monkeypatch.setattr(sys, "platform", "linux")
        linux = resolver._homebrew_candidates()
        assert "/home/linuxbrew/.linuxbrew/bin/sg" in linux
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolver._homebrew_candidates() == []


class TestCaching:
    def test_resolution_is_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resolver, "_runtime_candidates", list)
        monkeypatch.setattr(resolver, "_skill_bin_candidates", list)
        monkeypatch.setattr(resolver, "_path_candidates", list)
        monkeypatch.setattr(resolver, "_homebrew_candidates", list)
        binary = _make_executable(tmp_path / "sg")
        monkeypatch.setenv("SHERRY_SG_PATH", str(binary))
        assert resolver.resolve_sg_binary() == str(binary)
        monkeypatch.delenv("SHERRY_SG_PATH")
        assert resolver.resolve_sg_binary() == str(binary)
