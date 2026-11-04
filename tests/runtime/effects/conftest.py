"""Fixtures for the restart-recovery effect queue tests.

Each test gets its own SQLite file by repointing the module-level namespace the
``BaseSQLiteRepository`` reads (the store receives ``globals()``). The process
wide ``_sync_tables_ready`` flag and the cached store are reset too, so one
test can never see another's schema/rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def effects_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import runtime.effects.intent_log as intent_log

    db_path = tmp_path / "effects.db"
    monkeypatch.setattr(intent_log, "_DB_DIR", tmp_path)
    monkeypatch.setattr(intent_log, "_DB_PATH", db_path)
    monkeypatch.setattr(intent_log, "_sync_tables_ready", False)
    return db_path
