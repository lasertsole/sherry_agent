"""Shared DB isolation fixtures for the message-persistence tests."""

from __future__ import annotations

import uuid

import pytest

from context_engine.store import core as store_core
from context_engine.store import db as store_db
from runtime import state_register_mem


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


@pytest.fixture
def sid(request: pytest.FixtureRequest) -> str:
    value = "msg-persist-" + request.node.name[:32] + "-" + uuid.uuid4().hex[:6]
    yield value
    state_register_mem.clear_session(value)
