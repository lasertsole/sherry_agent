"""The desktop backend builder's version contract.

``packaging/build_backend.py`` stamps a ``VERSION`` marker into the built tree,
and the desktop shell (``client/src-tauri/src/services/backend_downloader.rs``)
re-downloads the backend whenever that marker differs from the version the
installer itself reports. The two sides must therefore read the ONE version the
installer is built from: ``client/src-tauri/tauri.conf.json``'s ``version``
field, which Tauri hands back to the shell as ``package_info().version``.

These tests pin that source and the marker's format. Deriving it from a git tag
or a branch name would make the shell re-download the backend on every launch.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]
TAURI_CONF = ROOT / "client" / "src-tauri" / "tauri.conf.json"
CARGO_TOML = ROOT / "client" / "src-tauri" / "Cargo.toml"


def _load_build_backend():
    """Import ``packaging/build_backend.py`` by path (it is a script, not a package)."""
    path = ROOT / "packaging" / "build_backend.py"
    spec = importlib.util.spec_from_file_location("packaging_build_backend", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_installer_version_is_the_tauri_config_version():
    module = _load_build_backend()
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))

    assert module.installer_version() == config["version"]
    assert module.installer_version().strip() == module.installer_version()


def test_the_config_version_matches_the_crate_version():
    # The shell compares its own build version against the marker; Tauri takes
    # that version from this config (falling back to Cargo.toml), so a drift
    # between the two files would ship a marker nobody matches.
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    manifest = tomllib.loads(CARGO_TOML.read_text(encoding="utf-8"))

    assert config["version"] == manifest["package"]["version"]


def test_the_version_marker_is_a_single_trimmed_line(tmp_path: Path):
    module = _load_build_backend()

    marker = module.write_version_marker(tmp_path, "1.2.3")

    assert marker == tmp_path / "VERSION"
    assert marker.read_text(encoding="utf-8") == "1.2.3\n"


def test_the_marker_defaults_to_the_installer_version(tmp_path: Path):
    module = _load_build_backend()

    marker = module.write_version_marker(tmp_path)

    assert marker.read_text(encoding="utf-8") == f"{module.installer_version()}\n"


def test_a_missing_version_falls_back_to_a_placeholder(tmp_path: Path, monkeypatch):
    # A malformed config must not crash the build; the shell then treats the
    # tree as stale and re-downloads, which is the safe direction.
    module = _load_build_backend()
    empty = tmp_path / "tauri.conf.json"
    empty.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "TAURI_CONF", empty)

    assert module.installer_version() == "0.0.0"
