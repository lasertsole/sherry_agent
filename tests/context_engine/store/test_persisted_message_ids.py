"""Persistence watermark store API — ``persisted_message_ids``.

The compaction middleware flushes a discarded prefix at most once; this table
is the cross-restart half of that write-once guarantee (the in-process
``_db_persisted`` marker is lost when graph state is deserialized). Pinned here:
idempotent marking, session scoping, and cleanup on session deletion.
"""

import pytest

from context_engine.store import core as store_core
from context_engine.store import db as store_db

pytestmark = [pytest.mark.unit]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


class TestWatermarkStore:
    def test_mark_and_filter_are_idempotent(self, isolated_db):
        assert store_core.mark_message_ids_persisted("wm", ["m1", "m2"]) == 2
        assert store_core.mark_message_ids_persisted("wm", ["m2", "m3"]) == 1
        assert store_core.filter_persisted_message_ids("wm", ["m1", "m3", "m9"]) == {"m1", "m3"}

    def test_session_scoping_and_delete(self, isolated_db):
        store_core.mark_message_ids_persisted("wm-a", ["m1"])
        store_core.mark_message_ids_persisted("wm-b", ["m1"])
        assert store_core.filter_persisted_message_ids("wm-a", ["m1"]) == {"m1"}

        store_core.delete_messages_by_session("wm-a")
        assert store_core.filter_persisted_message_ids("wm-a", ["m1"]) == set()
        assert store_core.filter_persisted_message_ids("wm-b", ["m1"]) == {"m1"}

    def test_empty_inputs_are_noops(self, isolated_db):
        assert store_core.mark_message_ids_persisted("wm-empty", []) == 0
        assert store_core.filter_persisted_message_ids("wm-empty", []) == set()
