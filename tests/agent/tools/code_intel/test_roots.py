"""Index-root resolution: the per-project DB lives under ``.sherry/``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.code_intel.roots import DB_ENV_KEY, index_db_for_root
from config.features import CODE_INTEL

pytestmark = [pytest.mark.unit]


def test_a_project_root_indexes_into_dot_sherry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DB_ENV_KEY, raising=False)

    path = index_db_for_root(tmp_path, "", tmp_path / "shared")

    assert path == str(tmp_path / ".sherry" / "codeintel" / "index.db")


def test_an_explicit_config_wins_over_the_project_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DB_ENV_KEY, raising=False)

    assert index_db_for_root(tmp_path, "/pinned/index.db", tmp_path) == "/pinned/index.db"


def test_the_env_override_wins_over_everything(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(DB_ENV_KEY, "/env/index.db")

    assert index_db_for_root(tmp_path, "/pinned/index.db", tmp_path) == "/env/index.db"


def test_no_project_root_uses_the_shared_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DB_ENV_KEY, raising=False)

    assert index_db_for_root(None, "", tmp_path / "shared") == str(tmp_path / "shared" / "index.db")


def test_the_indexer_prunes_the_whole_dot_sherry_tree() -> None:
    prune = CODE_INTEL["code_intel_prune_dirs"]

    assert ".sherry" in prune
    assert ".codeintel" not in prune
