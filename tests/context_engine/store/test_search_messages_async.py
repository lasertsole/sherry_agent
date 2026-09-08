"""TDD tests for audit issue #14 — blocking lock + eager connection in async context.

Audit findings being pinned by these tests:

* ``context_engine/core.py`` opened the SQLite connection at import time
  (module-level ``_db = get_db()``) — an import side effect plus a shared
  connection for every consumer.
* ``search_messages`` is blocking (``threading.Lock`` + sqlite3 I/O); calling
  it from a coroutine stalls the event loop.

Contract after the fix:

1. Importing ``context_engine.core`` opens NO database connection — the
   connection is created lazily on first use.
2. ``search_messages_async`` offloads the blocking search to an executor
   thread so the event loop stays responsive while a search runs.
3. The async wrapper returns exactly what the sync path returns.
"""

import asyncio
import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import context_engine.core as core_mod

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SESSION_ID = "test_search_async_session"

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


@pytest.fixture
def fts5_db():
    """Minimal production-shaped SQLite db: messages + FTS5 insert trigger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = sqlite3.connect(
            str(Path(tmpdir) / "test_mes_memory.db"),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_num INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_status TEXT,
                tool_name TEXT,
                timestamp TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE messages_fts USING fts5(content);
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (
                    new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                );
            END;
        """)
        rows = [
            (1, "human", json.dumps("how do I use docker compose?", ensure_ascii=False)),
            (1, "ai", json.dumps("Docker compose runs multi-container apps.", ensure_ascii=False)),
            (2, "human", json.dumps("what about kubernetes?", ensure_ascii=False)),
            (
                2,
                "ai",
                json.dumps(
                    "Kubernetes orchestrates containers at cluster scale.", ensure_ascii=False
                ),
            ),
        ]
        for turn_num, role, content in rows:
            db.execute(
                "INSERT INTO messages (turn_num, session_id, role, content, timestamp)"
                " VALUES (?, ?, ?, ?, ?)",
                (turn_num, SESSION_ID, role, content, "20260905120000"),
            )
        db.commit()
        yield {"db": db, "session_id": SESSION_ID}
        db.close()


@pytest.fixture
def patched_core_db(fts5_db, monkeypatch):
    """Point the module singleton at the temp db (mirrors production patching)."""
    monkeypatch.setattr(core_mod, "_db", fts5_db["db"])
    monkeypatch.setattr(core_mod, "_lock", threading.Lock())
    return fts5_db


class TestLazyConnection:
    def test_import_does_not_open_connection(self):
        """Importing context_engine.core must not create the DB connection."""
        code = (
            "import context_engine.core as core\n"
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
            f"import-time side effect detected:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "LAZY_OK" in result.stdout

    def test_first_use_creates_connection_lazily(self, patched_core_db):
        """After a patched-None singleton, the first search materializes a connection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_get_db = None
            import context_engine.store as store_mod

            real_get_db = store_mod.get_db

            def temp_get_db():
                conn = sqlite3.connect(
                    str(Path(tmpdir) / "lazy.db"),
                    check_same_thread=False,
                    timeout=1.0,
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                return conn

            core_mod._db = None
            try:
                with pytest.MonkeyPatch.context() as mp:
                    mp.setattr(store_mod, "get_db", temp_get_db)
                    results = core_mod.search_messages("docker", patched_core_db["session_id"])
                assert core_mod._db is not None, "lazy connection must be cached after first use"
                assert isinstance(results, list)
            finally:
                core_mod._db = None
                assert store_mod.get_db is real_get_db


class TestAsyncSearch:
    pytestmark = pytest.mark.asyncio

    async def test_async_returns_same_results_as_sync(self, patched_core_db):
        """search_messages_async must agree with the sync path."""
        sid = patched_core_db["session_id"]

        sync_results = core_mod.search_messages("docker", sid)
        async_results = await core_mod.search_messages_async("docker", sid)

        assert sync_results, "sync search must find the docker messages"
        assert async_results == sync_results

    async def test_async_search_keeps_event_loop_responsive(self, patched_core_db, monkeypatch):
        """A slow search via the async wrapper must NOT block the event loop."""
        sid = patched_core_db["session_id"]
        real_search = core_mod.search_messages

        def slow_search(*args, **kwargs):
            time.sleep(0.3)
            return real_search(*args, **kwargs)

        monkeypatch.setattr(core_mod, "search_messages", slow_search)

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        try:
            results = await core_mod.search_messages_async("docker", sid)
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task

        assert results, "async search must still return the docker messages"
        assert ticks >= 5, (
            f"event loop was blocked during search: only {ticks} ticks in 0.3s "
            "(executor offload missing?)"
        )

    async def test_async_empty_query(self, patched_core_db):
        assert await core_mod.search_messages_async("   ", patched_core_db["session_id"]) == []
