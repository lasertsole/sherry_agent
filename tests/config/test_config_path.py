"""Unit tests for config/path.py — path constants and resolution."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.unit]


class TestRootDir:
    """Test ROOT_DIR resolution."""

    def test_root_dir_is_path_instance(self):
        from config.path import ROOT_DIR

        assert isinstance(ROOT_DIR, Path)

    def test_root_dir_resolves_to_absolute(self):
        from config.path import ROOT_DIR

        assert ROOT_DIR.is_absolute()

    def test_root_dir_points_to_project_root(self):
        from config.path import ROOT_DIR

        # ROOT_DIR should be the parent of the config package
        assert (ROOT_DIR / "config").exists()


class TestSubPaths:
    """Test derived path constants."""

    def test_src_dir_is_subdir_of_root(self):
        from config.path import ROOT_DIR, SRC_DIR

        assert SRC_DIR == ROOT_DIR / "src"

    def test_skills_dir_is_subdir_of_root(self):
        from config.path import ROOT_DIR, SKILLS_DIR

        assert SKILLS_DIR == ROOT_DIR / "skills"

    def test_auto_skills_dir_is_subdir_of_skills(self):
        from config.path import SKILLS_DIR, AUTO_SKILLS_DIR

        assert AUTO_SKILLS_DIR == SKILLS_DIR / "auto/"

    def test_workspace_dir_is_subdir_of_root(self):
        from config.path import ROOT_DIR, WORKSPACE_DIR

        assert WORKSPACE_DIR == ROOT_DIR / "workspace"

    def test_models_dir_is_subdir_of_root(self):
        from config.path import ROOT_DIR, MODELS_DIR

        assert MODELS_DIR == ROOT_DIR / "models"

    def test_temp_dir_is_subdir_of_root(self):
        from config.path import ROOT_DIR, TEMP_DIR

        assert TEMP_DIR == ROOT_DIR / "temp"

    def test_sessions_dir_is_subdir_of_workspace(self):
        from config.path import WORKSPACE_DIR, SESSIONS_DIR

        assert SESSIONS_DIR == WORKSPACE_DIR / "sessions"

    def test_static_dir_is_subdir_of_root(self):
        from config.path import ROOT_DIR, STATIC_DIR

        assert STATIC_DIR == ROOT_DIR / "static"

    def test_env_path_is_file(self):
        from config.path import ROOT_DIR, ENV_PATH

        assert ENV_PATH == ROOT_DIR / ".env"

    def test_interpreter_path(self):
        from config.path import INTERPRETER_PATH

        # The constant must track the interpreter actually running
        # this process (cross-platform by construction: Windows Scripts/,
        # POSIX bin/, conda, system python) instead of a Windows-only venv
        # layout. The old constant pointed at a file that never existed on
        # disk — Windows venvs ship python.exe and POSIX venvs use bin/.
        assert INTERPRETER_PATH == Path(sys.executable)
        assert INTERPRETER_PATH.exists()

    def test_context_engine_path(self):
        from config.path import ROOT_DIR, CONTEXT_ENGINE_PATH

        assert CONTEXT_ENGINE_PATH == ROOT_DIR / "context_engine"

    def test_plugins_path(self):
        from config.path import ROOT_DIR, PLUGINS_PATH

        assert PLUGINS_PATH == ROOT_DIR / "plugins"

    def test_workspace_template_dir(self):
        from config.path import WORKSPACE_DIR, WORKSPACE_TEMPLATE_DIR

        assert WORKSPACE_TEMPLATE_DIR == WORKSPACE_DIR / "template"

    def test_workspace_template_langs(self):
        from config.path import WORKSPACE_TEMPLATE_LANGS

        assert WORKSPACE_TEMPLATE_LANGS == ("zh", "en", "ja", "ko")

    def test_default_workspace_template_lang(self):
        from config.path import DEFAULT_WORKSPACE_TEMPLATE_LANG

        assert DEFAULT_WORKSPACE_TEMPLATE_LANG == "en"

    def test_heartbeat_template_path(self):
        from config.path import WORKSPACE_TEMPLATE_DIR, HEARTBEAT_TEMPLATE_PATH

        assert HEARTBEAT_TEMPLATE_PATH == WORKSPACE_TEMPLATE_DIR / "HEARTBEAT.md"

    def test_knowledge_dir(self):
        from config.path import WORKSPACE_DIR, KNOWLEDGE_DIR

        assert KNOWLEDGE_DIR == WORKSPACE_DIR / "knowledge"

    def test_memory_dir(self):
        from config.path import WORKSPACE_DIR, MEMORY_DIR

        assert MEMORY_DIR == WORKSPACE_DIR / "memory"

    def test_heartbeat_path(self):
        from config.path import WORKSPACE_DIR, HEARTBEAT_PATH

        assert HEARTBEAT_PATH == WORKSPACE_DIR / "HEARTBEAT.md"

    def test_memory_index_dir(self):
        from config.path import MEMORY_DIR, MEMORY_INDEX_DIR

        assert MEMORY_INDEX_DIR == MEMORY_DIR / "index"

    def test_knowledge_index_dir(self):
        from config.path import KNOWLEDGE_DIR, KNOWLEDGE_INDEX_DIR

        assert KNOWLEDGE_INDEX_DIR == KNOWLEDGE_DIR / "index"

    def test_plan_knowledge_dir(self):
        from config.path import KNOWLEDGE_DIR, PLAN_KNOWLEDGE_DIR

        assert PLAN_KNOWLEDGE_DIR == KNOWLEDGE_DIR / "plans"


class TestTemplateLangResolution:
    """Test workspace template language resolution with fallback."""

    def test_resolve_returns_default_when_none(self):
        from config.path import (
            DEFAULT_WORKSPACE_TEMPLATE_LANG,
            resolve_workspace_template_lang,
        )

        assert resolve_workspace_template_lang() == DEFAULT_WORKSPACE_TEMPLATE_LANG

    def test_resolve_valid_lang(self, tmp_path, monkeypatch):
        from config.path import resolve_workspace_template_lang

        # Create an existing lang dir
        (tmp_path / "en").mkdir()
        monkeypatch.setattr("config.path.DEFAULT_WORKSPACE_TEMPLATE_LANG", "zh")
        monkeypatch.setattr("config.path.WORKSPACE_TEMPLATE_DIR", tmp_path)
        assert resolve_workspace_template_lang("en") == "en"

    def test_resolve_falls_back_when_dir_missing(self, tmp_path, monkeypatch):
        from config.path import (
            resolve_workspace_template_lang,
        )

        # lang dir does not exist -> fall back to default
        monkeypatch.setattr("config.path.DEFAULT_WORKSPACE_TEMPLATE_LANG", "zh")
        monkeypatch.setattr("config.path.WORKSPACE_TEMPLATE_DIR", tmp_path)
        assert resolve_workspace_template_lang("fr") == "zh"

    def test_resolve_dir_appends_lang(self, tmp_path, monkeypatch):
        from config.path import (
            resolve_workspace_template_dir,
        )

        (tmp_path / "en").mkdir()
        monkeypatch.setattr("config.path.WORKSPACE_TEMPLATE_DIR", tmp_path)
        assert resolve_workspace_template_dir("en") == tmp_path / "en"

    def test_resolve_dir_falls_back_to_default(self, tmp_path, monkeypatch):
        from config.path import (
            resolve_workspace_template_dir,
        )

        monkeypatch.setattr("config.path.DEFAULT_WORKSPACE_TEMPLATE_LANG", "zh")
        monkeypatch.setattr("config.path.WORKSPACE_TEMPLATE_DIR", tmp_path)
        assert resolve_workspace_template_dir("fr") == tmp_path / "zh"


class TestAppHome:
    """``SHERRY_HOME`` moves every writable path; unset keeps the old layout.

    The constants are computed at import time, so both cases run in a
    SUBPROCESS — importing ``config`` in-process would only ever exercise the
    interpreter's own environment.
    """

    def _run(self, env_home: str | None, expression: str) -> str:
        env = dict(os.environ)
        env.pop("SHERRY_HOME", None)
        if env_home is not None:
            env["SHERRY_HOME"] = env_home
        out = subprocess.run(
            [sys.executable, "-c", expression],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        return out.stdout.strip()

    def test_unset_app_home_is_the_code_root(self):
        out = self._run(
            None,
            "from config.path import APP_HOME, ROOT_DIR; print(APP_HOME == ROOT_DIR)",
        )
        assert out == "True"

    def test_set_app_home_moves_the_writable_tree(self, tmp_path):
        home = tmp_path / "home"
        out = self._run(
            str(home),
            "from config.path import (APP_HOME, ENV_PATH, LOG_DIR, SRC_DIR, "
            "USER_SKILLS_DIR, WORKSPACE_DIR, app_relative); "
            "print(APP_HOME); print(ENV_PATH.parent); print(WORKSPACE_DIR.parent); "
            "print(SRC_DIR.parent); print(LOG_DIR.parents[1]); print(USER_SKILLS_DIR.parent); "
            "print(app_relative(USER_SKILLS_DIR / 'auto'))",
        )
        lines = out.splitlines()
        assert all(line == str(home) for line in lines[:6]), out
        # Locations keep the CHECKOUT shape whichever home they live in.
        assert lines[6] == "skills/auto"

    def test_first_run_seeds_env_and_sherry_jsonc(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        out = self._run(
            str(home),
            "from config.path import ENV_PATH; import config.sherry_settings as s; "
            "print(ENV_PATH.read_text(encoding='utf-8') != ''); "
            "print(s.SHERRY_CONFIG_PATH.read_text(encoding='utf-8') != '')",
        )
        assert out.splitlines() == ["True", "True"]
        assert (home / ".env").is_file() and (home / "sherry.jsonc").is_file()

    def test_the_skills_loader_scans_both_roots(self, tmp_path):
        home = tmp_path / "home"
        out = self._run(
            str(home),
            "from config.path import skills_loader_roots, SKILLS_DIR, USER_SKILLS_DIR; "
            "print(len(skills_loader_roots())); print(SKILLS_DIR in skills_loader_roots()); "
            "print(USER_SKILLS_DIR in skills_loader_roots())",
        )
        assert out.splitlines() == ["2", "True", "True"]
