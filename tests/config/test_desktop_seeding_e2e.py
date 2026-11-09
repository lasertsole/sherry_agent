"""End-to-end first-run seeding under a simulated PyInstaller onedir layout.

A subprocess gets ``sys._MEIPASS`` pointing at a fake ``_internal`` bundle and
``SHERRY_HOME`` at an empty app-data directory, then imports ``config``. The
four writable files must be seeded from the bundle templates; this exercises the
same import-time path a packaged Windows backend uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

ROOT = Path(__file__).resolve().parents[2]


def test_first_run_seeds_all_four_files_from_meipass(tmp_path: Path) -> None:
    bundle = tmp_path / "_internal"
    bundle.mkdir()
    (bundle / ".env").write_text("SHERRY_E2E_MARKER=1\n", encoding="utf-8")
    (bundle / "sherry.jsonc").write_text('{"LOG_LEVEL": "DEBUG"}\n', encoding="utf-8")
    (bundle / ".models.example").write_text(
        '{\n  "MAIN_LLM_PROVIDER": "openai"\n}\n', encoding="utf-8"
    )
    (bundle / ".network.example").write_text('{\n  "TAVILY_API_KEY": ""\n}\n', encoding="utf-8")
    app_home = tmp_path / "app-data"
    app_home.mkdir()

    script = """
import sys
sys._MEIPASS = sys.argv[1]
from config import path
from config import sherry_settings, models_settings, network_settings

for name in (".env", "sherry.jsonc", ".models", ".network"):
    target = path.APP_HOME / name
    assert target.is_file(), (name, target)
assert path.APP_HOME == path.APP_HOME.resolve()
assert sherry_settings.APP_HOME == path.APP_HOME
assert models_settings.APP_HOME == path.APP_HOME
assert network_settings.APP_HOME == path.APP_HOME
print(path.APP_HOME)
"""
    env = dict(os.environ)
    env["SHERRY_HOME"] = str(app_home)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script, str(bundle)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert (app_home / ".env").read_text(encoding="utf-8") == "SHERRY_E2E_MARKER=1\n"
    assert (app_home / "sherry.jsonc").is_file()
    assert (app_home / ".models").is_file()
    assert (app_home / ".network").is_file()


def test_first_run_seeds_from_the_sherry_home_cli_flag(tmp_path: Path) -> None:
    """The packaged shell passes ``--sherry-home`` (not the env var).

    This is the regression pin for the pre-0.1.7 bug: the model/network files
    resolved against the read-only bundle instead of the flag's app home, so
    the config panel rendered empty. All four files must land in the home.
    """
    bundle = tmp_path / "_internal"
    bundle.mkdir()
    (bundle / ".env").write_text("SHERRY_CLI_MARKER=1\n", encoding="utf-8")
    (bundle / "sherry.jsonc").write_text('{"LOG_LEVEL": "DEBUG"}\n', encoding="utf-8")
    (bundle / ".models.example").write_text(
        '{\n  "MAIN_LLM_PROVIDER": "openai"\n}\n', encoding="utf-8"
    )
    (bundle / ".network.example").write_text('{\n  "TAVILY_API_KEY": ""\n}\n', encoding="utf-8")
    app_home = tmp_path / "app-data"
    app_home.mkdir()

    script = """
import os, sys
sys._MEIPASS = os.environ["BUNDLE"]
sys.argv = ["sherry-backend", "--sherry-home", os.environ["APP_HOME"], "--sherry-desktop"]
from config import path
from config import sherry_settings, models_settings, network_settings

for name in (".env", "sherry.jsonc", ".models", ".network"):
    target = path.APP_HOME / name
    assert target.is_file(), (name, target)
assert path.APP_HOME == models_settings.APP_HOME == network_settings.APP_HOME
assert path.APP_HOME == sherry_settings.APP_HOME
print(path.APP_HOME)
"""
    env = dict(os.environ)
    env.pop("SHERRY_HOME", None)  # the flag is the only home channel here
    env["BUNDLE"] = str(bundle)
    env["APP_HOME"] = str(app_home)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    for name in (".env", "sherry.jsonc", ".models", ".network"):
        assert (app_home / name).is_file(), name
    # .env / sherry.jsonc already exist in the bundle as their own templates;
    # the two seeded FILES (not *.example) must never be written there.
    assert not (bundle / ".models").exists()
    assert not (bundle / ".network").exists()
