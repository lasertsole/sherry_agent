"""Desktop first-run seeding: frozen template roots and the four files.

The packaged backend is a PyInstaller onedir tree; its shipped templates live
under ``sys._MEIPASS`` / ``_internal``. A frozen module's synthetic ``__file__``
is not a reliable anchor, so ``resolve_code_root`` must prefer ``_MEIPASS``.
These tests pin that resolver and the seed functions that consume its result,
without importing a second copy of ``config`` or touching the real APP_HOME.
"""

from __future__ import annotations

import sys

import pytest

from config import models_settings as ms
from config import network_settings as ns
from config import path as config_path
from config import sherry_settings as ss
from config._home import resolve_code_root

pytestmark = [pytest.mark.unit]


def test_resolve_code_root_prefers_meipass(tmp_path, monkeypatch):
    pack_root = tmp_path / "_internal"
    pack_root.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(pack_root), raising=False)

    assert resolve_code_root(tmp_path / "synthetic") == pack_root.resolve()


def test_resolve_code_root_falls_back_without_meipass(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    code_root = tmp_path / "checkout"

    assert resolve_code_root(code_root) == code_root


def test_sherry_jsonc_seeds_from_the_resolved_template_root(tmp_path, monkeypatch):
    template_root = tmp_path / "_internal"
    template_root.mkdir()
    (template_root / "sherry.jsonc").write_text('{"LOG_LEVEL": "DEBUG"}\n', encoding="utf-8")
    target = tmp_path / "app" / "sherry.jsonc"
    monkeypatch.setattr(ss, "_TEMPLATE_ROOT", template_root)
    monkeypatch.setattr(ss, "SHERRY_CONFIG_PATH", target)

    ss.ensure_sherry_config_file()

    assert target.read_text(encoding="utf-8") == '{"LOG_LEVEL": "DEBUG"}\n'


def test_models_seeds_from_the_resolved_template_root(tmp_path, monkeypatch):
    template_root = tmp_path / "_internal"
    template_root.mkdir()
    (template_root / ".models.example").write_text(
        '{\n  "MAIN_LLM_PROVIDER": "openai"\n}\n', encoding="utf-8"
    )
    target = tmp_path / "app" / ".models"
    monkeypatch.setattr(ms, "_TEMPLATE_ROOT", template_root)
    monkeypatch.setattr(ms, "MODELS_CONFIG_TEMPLATE", template_root / ".models.example")
    monkeypatch.setattr(ms, "MODELS_CONFIG_PATH", target)
    monkeypatch.setattr(ms, "_legacy_env_values", lambda: {})

    ms.ensure_models_config_file()

    assert target.read_text(encoding="utf-8") == '{\n  "MAIN_LLM_PROVIDER": "openai"\n}\n'


def test_network_seeds_from_the_resolved_template_root(tmp_path, monkeypatch):
    template_root = tmp_path / "_internal"
    template_root.mkdir()
    (template_root / ".network.example").write_text(
        '{\n  "TAVILY_API_KEY": ""\n}\n', encoding="utf-8"
    )
    target = tmp_path / "app" / ".network"
    monkeypatch.setattr(ns, "_TEMPLATE_ROOT", template_root)
    monkeypatch.setattr(ns, "NETWORK_CONFIG_TEMPLATE", template_root / ".network.example")
    monkeypatch.setattr(ns, "NETWORK_CONFIG_PATH", target)
    monkeypatch.setattr(ns, "_legacy_env_values", lambda: {})
    monkeypatch.setattr(ns, "_legacy_langsmith_values", lambda: {})

    ns.ensure_network_config_file()

    assert target.read_text(encoding="utf-8") == '{\n  "TAVILY_API_KEY": ""\n}\n'


def test_env_seeds_from_the_resolved_template_root(tmp_path, monkeypatch):
    template_root = tmp_path / "_internal"
    template_root.mkdir()
    (template_root / ".env").write_text("SHERRY_GATEWAY_PORT=8080\n", encoding="utf-8")
    code_root = tmp_path / "checkout"
    app_home = tmp_path / "app"
    app_home.mkdir()
    env_path = app_home / ".env"
    monkeypatch.setattr(config_path, "_ENV_TEMPLATE_ROOT", template_root)
    monkeypatch.setattr(config_path, "ROOT_DIR", code_root)
    monkeypatch.setattr(config_path, "APP_HOME", app_home)
    monkeypatch.setattr(config_path, "ENV_PATH", env_path)

    config_path._seed_env_file()

    assert env_path.read_text(encoding="utf-8") == "SHERRY_GATEWAY_PORT=8080\n"
