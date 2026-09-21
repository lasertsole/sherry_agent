"""TDD tests for audit #14 in ``context_engine/store/core.py``: lazy connection.

The store CRUD module previously executed ``_db = get_db()`` at import time, so
importing it opened the SQLite connection as a side effect. Contract pinned
here:

1. Importing ``context_engine.store.core`` opens no connection (``_db is None``).
2. The first store call materializes the connection through ``get_db()`` and
   caches it on the module global, so later calls reuse it.
3. Tests can still inject a connection by patching ``core._db`` (the accessor
   returns the patched object without calling ``get_db``).
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import context_engine.store.core as store_core

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())


def _make_messages_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_num INTEGER)"
    )
    return conn


class TestStoreCoreLazyConnection:
    def test_import_does_not_open_connection(self):
        """Importing the store module must not create the DB connection."""
        code = (
            "import context_engine.store.core as core\n"
            "assert core._db is None, (\n"
            "    f'module import eagerly opened a DB connection: {core._db!r}'\n"
            ")\n"
            "print('LAZY_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"import-time connection detected:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "LAZY_OK" in result.stdout

    def test_first_use_materializes_and_caches_once(self, monkeypatch, tmp_path):
        """Given a cold singleton, When a store call runs, Then get_db runs once and is cached."""
        conn = _make_messages_db(tmp_path / "lazy_store.db")
        calls: list[int] = []

        def counting_get_db() -> sqlite3.Connection:
            calls.append(1)
            return conn

        monkeypatch.setattr(store_core, "_db", None)
        monkeypatch.setattr(store_core, "get_db", counting_get_db)

        assert store_core.get_max_turn_num("lazy-session") == 0
        assert store_core._db is conn
        assert calls == [1]

        assert store_core.get_max_turn_num("lazy-session") == 0
        assert calls == [1], "the cached connection must be reused, not re-created"
        conn.close()

    def test_patched_connection_is_returned_without_get_db(self, monkeypatch, tmp_path):
        """Given an injected connection, When a store call runs, Then get_db is never called."""
        conn = _make_messages_db(tmp_path / "injected.db")

        def exploding_get_db() -> sqlite3.Connection:
            raise AssertionError("get_db must not be called when _db is injected")

        monkeypatch.setattr(store_core, "_db", conn)
        monkeypatch.setattr(store_core, "get_db", exploding_get_db)

        assert store_core.get_max_turn_num("injected-session") == 0
        conn.close()
