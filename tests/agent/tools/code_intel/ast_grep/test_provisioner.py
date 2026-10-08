"""Unit tests for the SHA-256-verified ast-grep provisioner."""

from __future__ import annotations

import copy
import hashlib
import io
import sys
import zipfile
from pathlib import Path

import pytest

from agent.tools.code_intel.ast_grep import provisioner
from config.features import AST_GREP

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _config_for(slug: str, sha: str, url: str = "https://example.invalid/app.zip") -> dict:
    config = copy.deepcopy(AST_GREP)
    config["ast_grep_release_assets"] = {slug: {"url": url, "sha256": sha}}
    return config


@pytest.fixture(autouse=True)
def _clean_cache():
    provisioner._clear_cache_for_tests()
    yield
    provisioner._clear_cache_for_tests()


class TestExtractPreference:
    def test_prefers_real_binary_over_sg_launcher(self) -> None:
        archive = _zip_bytes({"sg": b"LAUNCHER", "ast-grep": b"REALBINARY"})
        assert provisioner._extract_binary(archive, "linux") == b"REALBINARY"

    def test_falls_back_to_sg_when_ast_grep_absent(self) -> None:
        archive = _zip_bytes({"sg": b"LAUNCHER"})
        assert provisioner._extract_binary(archive, "linux") == b"LAUNCHER"

    def test_raises_when_no_binary_present(self) -> None:
        archive = _zip_bytes({"README.md": b"nothing here"})
        with pytest.raises(FileNotFoundError):
            provisioner._extract_binary(archive, "linux")


class TestProvision:
    def test_success_writes_verified_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = _zip_bytes({"sg": b"LAUNCHER", "ast-grep": b"REALBINARY"})
        sha = hashlib.sha256(archive).hexdigest()
        monkeypatch.setattr(provisioner, "AST_GREP", _config_for("test-slug", sha))
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path)
        monkeypatch.setattr(provisioner, "_download", lambda url, timeout: archive)

        result = provisioner.provision_sg_binary()

        assert result == str(tmp_path / "sg")
        installed = tmp_path / "sg"
        assert installed.read_bytes() == b"REALBINARY"
        if sys.platform != "win32":
            assert installed.stat().st_mode & 0o777 == 0o755

    def test_checksum_mismatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = _zip_bytes({"ast-grep": b"REALBINARY"})
        monkeypatch.setattr(provisioner, "AST_GREP", _config_for("test-slug", "0" * 64))
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path)
        monkeypatch.setattr(provisioner, "_download", lambda url, timeout: archive)

        assert provisioner.provision_sg_binary() is None
        assert not (tmp_path / "sg").exists()

    def test_download_timeout_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioner, "AST_GREP", _config_for("test-slug", "a" * 64))
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path)

        def _boom(url: str, timeout: float) -> bytes:
            raise TimeoutError("timed out")

        monkeypatch.setattr(provisioner, "_download", _boom)
        assert provisioner.provision_sg_binary() is None
        assert not (tmp_path / "sg").exists()

    def test_download_error_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioner, "AST_GREP", _config_for("test-slug", "a" * 64))
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path)

        def _boom(url: str, timeout: float) -> bytes:
            raise OSError("connection refused")

        monkeypatch.setattr(provisioner, "_download", _boom)
        assert provisioner.provision_sg_binary() is None

    def test_missing_asset_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioner, "AST_GREP", _config_for("other-slug", "a" * 64))
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path)
        assert provisioner.provision_sg_binary() is None

    def test_bad_zip_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"not a zip"
        sha = hashlib.sha256(payload).hexdigest()
        monkeypatch.setattr(provisioner, "AST_GREP", _config_for("test-slug", sha))
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path)
        monkeypatch.setattr(provisioner, "_download", lambda url, timeout: payload)
        assert provisioner.provision_sg_binary() is None
        assert not (tmp_path / "sg").exists()


class TestConfigIntegrity:
    def test_six_verified_platform_hashes(self) -> None:
        assets = AST_GREP["ast_grep_release_assets"]
        assert set(assets) == {
            "darwin-arm64",
            "darwin-x64",
            "linux-arm64",
            "linux-x64",
            "win32-arm64",
            "win32-x64",
        }
        for slug, asset in assets.items():
            sha = asset["sha256"]
            assert len(sha) == 64, slug
            assert all(ch in "0123456789abcdef" for ch in sha), slug
            assert AST_GREP["ast_grep_pinned_version"] in asset["url"], slug

    def test_pinned_version(self) -> None:
        assert AST_GREP["ast_grep_pinned_version"] == "0.43.0"
