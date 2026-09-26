"""Env-loading contract of the nudge_extraction eval suite.

The suite must source its API keys from the eval-local ``.env`` next to
``suite.py`` — never from the production repo-root ``.env``: an eval run must
not silently gain access to live credentials. Keys may also come from the
process environment, which stays authoritative (``override=False``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import evals.nudge_extraction.suite as suite

pytestmark = [pytest.mark.unit]


def test_eval_env_file_is_suite_local_not_repo_root():
    path = suite._eval_env_path()
    assert path.name == ".env"
    assert path.parent == Path(suite.__file__).resolve().parent
    assert path != suite.REPO_ROOT / ".env"


def test_load_eval_env_reads_the_suite_local_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MAIN_LLM_API_KEY=eval-local-value\n", encoding="utf-8")
    monkeypatch.setattr(suite, "_eval_env_path", lambda: env_file)
    monkeypatch.delenv("MAIN_LLM_API_KEY", raising=False)

    loaded = suite._load_eval_env()

    assert loaded == env_file
    assert os.environ["MAIN_LLM_API_KEY"] == "eval-local-value"


def test_load_eval_env_keeps_process_env_authoritative(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AUXILIARY_LLM_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setattr(suite, "_eval_env_path", lambda: env_file)
    monkeypatch.setenv("AUXILIARY_LLM_API_KEY", "from-process")

    suite._load_eval_env()

    assert os.environ["AUXILIARY_LLM_API_KEY"] == "from-process"
