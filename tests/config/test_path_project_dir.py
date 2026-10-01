"""The configurable process-level default root of the project-directory feature.

``resolve_default_project_dir`` answers "where does the agent work when a
session has no explicit binding": ``SHERRY_PROJECT_DIR`` → the ``project_dir``
key of ``sherry.jsonc`` → ``ROOT_DIR``. Two contracts are pinned here:

1. **No configuration at all means byte-for-byte the old behaviour** (ROOT_DIR),
   so this phase changes nothing for existing installs.
2. **A bad configured value never bricks the boot** — it falls back with a
   warning, and ``validate_project_dir`` (the strict input validator used by
   the session API later) is the one that raises.
"""

from __future__ import annotations

import pathlib

import pytest

import config.path as config_path
from config import path as path_mod
from config.sherry_settings import get_sherry_setting

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def caplog_warnings(monkeypatch) -> list[str]:
    """Capture loguru warning text (loguru is not wired into pytest's caplog)."""
    from loguru import logger as loguru_logger

    captured: list[str] = []

    def _capture(msg, *args, **kwargs) -> None:
        # Render loguru's lazy "{}" formatting so assertions can search the
        # values (loguru is not wired into pytest's caplog).
        try:
            captured.append(str(msg).format(*args))
        except (IndexError, KeyError):
            captured.append(str(msg))

    monkeypatch.setattr(loguru_logger, "warning", _capture, raising=False)
    return captured


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No inherited SHERRY_PROJECT_DIR, no leftover warn-once flag."""
    monkeypatch.delenv("SHERRY_PROJECT_DIR", raising=False)
    monkeypatch.setattr(path_mod, "_warned_invalid_project_dir", False)
    yield


def _write_sherry_jsonc(tmp_path: pathlib.Path, project_dir: str) -> pathlib.Path:
    cfg = tmp_path / "sherry.jsonc"
    cfg.write_text(f'{{ "project_dir": "{project_dir}" }}\n', encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# resolve_default_project_dir
# ---------------------------------------------------------------------------


def test_unset_everywhere_falls_back_to_the_repo_root():
    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR


def test_env_var_selects_the_directory(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(tmp_path))
    assert config_path.resolve_default_project_dir() == tmp_path.resolve()


def test_sherry_jsonc_key_selects_the_directory(tmp_path: pathlib.Path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()
    monkeypatch.setattr(
        path_mod, "get_sherry_setting", lambda key: str(target) if key == "project_dir" else ""
    )
    assert config_path.resolve_default_project_dir() == target.resolve()


def test_env_var_wins_over_the_jsonc_key(tmp_path: pathlib.Path, monkeypatch):
    from_env = tmp_path / "from-env"
    from_env.mkdir()
    from_jsonc = tmp_path / "from-jsonc"
    from_jsonc.mkdir()
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(from_env))
    monkeypatch.setattr(
        path_mod,
        "get_sherry_setting",
        lambda key: str(from_jsonc) if key == "project_dir" else "",
    )

    assert config_path.resolve_default_project_dir() == from_env.resolve()


def test_relative_value_falls_back_with_a_warning(tmp_path, monkeypatch, caplog_warnings):
    monkeypatch.setenv("SHERRY_PROJECT_DIR", "relative/path")

    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR
    assert any("Invalid SHERRY_PROJECT_DIR" in w for w in caplog_warnings)


def test_missing_directory_falls_back(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(tmp_path / "does-not-exist"))
    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR


def test_a_file_value_falls_back(tmp_path: pathlib.Path, monkeypatch):
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(target))
    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR


def test_the_warning_is_emitted_once_per_process(tmp_path, monkeypatch, caplog_warnings):
    monkeypatch.setenv("SHERRY_PROJECT_DIR", "relative/path")

    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR
    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR
    assert len([w for w in caplog_warnings if "Invalid SHERRY_PROJECT_DIR" in w]) == 1


def test_tilde_is_expanded(monkeypatch):
    monkeypatch.setenv("SHERRY_PROJECT_DIR", "~")
    home = pathlib.Path("~").expanduser().resolve()

    assert config_path.resolve_default_project_dir() == home


def test_symlink_to_a_directory_resolves_to_its_target(tmp_path: pathlib.Path, monkeypatch):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(link))

    assert config_path.resolve_default_project_dir() == target.resolve()


# ---------------------------------------------------------------------------
# validate_project_dir (strict: this one raises)
# ---------------------------------------------------------------------------


def test_validate_accepts_an_existing_absolute_directory(tmp_path: pathlib.Path):
    assert config_path.validate_project_dir(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize("bad", ["", "   ", "relative/path"])
def test_validate_rejects_empty_and_relative_values(bad: str):
    with pytest.raises(ValueError, match="project directory"):
        config_path.validate_project_dir(bad)


def test_validate_rejects_a_missing_path(tmp_path: pathlib.Path):
    with pytest.raises(ValueError, match="cannot be resolved"):
        config_path.validate_project_dir(tmp_path / "nope")


def test_validate_rejects_a_file(tmp_path: pathlib.Path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        config_path.validate_project_dir(target)


def test_validate_surfaces_a_symlink_loop(tmp_path: pathlib.Path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)

    with pytest.raises(ValueError):
        config_path.validate_project_dir(loop)


# ---------------------------------------------------------------------------
# The sherry.jsonc registry entry
# ---------------------------------------------------------------------------


def test_project_dir_is_a_registered_setting():
    """The key is part of the typed registry, so the config API can write it."""
    assert get_sherry_setting("project_dir") == ""


def test_jsonc_value_reaches_the_resolver(tmp_path: pathlib.Path, monkeypatch):
    """End-to-end through the real loader (file -> settings -> resolver)."""
    from config import sherry_settings

    target = tmp_path / "from-file"
    target.mkdir()
    monkeypatch.setattr(
        sherry_settings, "SHERRY_CONFIG_PATH", _write_sherry_jsonc(tmp_path, str(target))
    )

    assert config_path.resolve_default_project_dir() == target.resolve()
