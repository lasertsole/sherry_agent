"""Fixtures for tests/agent/tools/todolist/.

``isolated_db`` points the todolist store module at a per-test tmp SQLite file
and resets its once-per-process init state, mirroring
``tests/agent/tools/taskflow/conftest.py`` (which in turn mirrors the subagent
blueprint). The real ``agent/tools/todolist/data/todos.db`` is never touched.
"""

import asyncio
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the todolist store module at a tmp_path db and reset init state."""
    from agent.tools.todolist.registry import store_sqlite

    db_path = tmp_path / "todos.db"
    monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
    monkeypatch.setattr(store_sqlite, "_DB_PATH", db_path)
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    # Fresh lock per test: a contended acquire BINDS an asyncio.Lock to the
    # acquiring test's event loop, and pytest-asyncio creates a new loop per test.
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    return db_path
