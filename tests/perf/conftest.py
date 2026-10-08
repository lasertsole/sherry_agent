"""Fixtures for the performance suite.

Directory-local by necessity: ``isolated_db`` lives in the taskflow suite's
conftest, and a fixture defined there is invisible to another test directory.
The body mirrors it exactly — same tmp redirection, same fresh init lock per
test (a contended acquire binds an asyncio.Lock to the acquiring loop, and each
test gets a new loop).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the taskflow store module at a per-test tmp SQLite file."""
    from agent.tools.taskflow.registry import store_sqlite

    db_path = tmp_path / "taskflow_registry.db"
    monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
    monkeypatch.setattr(store_sqlite, "_DB_PATH", db_path)
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    return db_path
