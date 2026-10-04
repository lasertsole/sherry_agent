"""Isolation for the file-tool tests: never touch the real snapshot store.

The pre-write snapshot store is process-level state (it reads its database path
from its own module globals, like every other SQLite store here), so a test that
exercises the write path would otherwise append rows to the repository's
``src/data/file-snapshots.db``. This autouse fixture repoints both the database
and the blob directory at the test's own tmp tree.
"""

import pytest

from agent.tools.file_tools import snapshot as snapshot_mod
from config import path as path_mod


@pytest.fixture(autouse=True)
def isolated_snapshot_store(tmp_path, monkeypatch):
    """Point the snapshot database and blob tree at a per-test directory."""
    db_dir = tmp_path / "snapshot-db"
    monkeypatch.setattr(snapshot_mod, "_DB_DIR", db_dir)
    monkeypatch.setattr(snapshot_mod, "_DB_PATH", db_dir / "file-snapshots.db")
    monkeypatch.setattr(snapshot_mod, "_sync_tables_ready", False)
    monkeypatch.setattr(path_mod, "SESSIONS_DIR", tmp_path / "sessions")
    yield
