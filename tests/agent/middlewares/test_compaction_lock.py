"""P0-3: SQLite compaction lock prevents concurrent session compaction.

The lock serializes per-session compactions at the storage level; a failed
acquire must fail open (compression skipped, request returned untouched).
"""

import sqlite3
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

import agent.middlewares.summarization as summarization_module
import context_engine.store.db as mes_memory_store
from agent.middlewares.compaction_lock import CompactionLock, CompactionLockError

pytestmark = [pytest.mark.unit]

_SID = "p03-session"


@pytest.fixture
def lock_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(mes_memory_store, "_db_path", db_path)
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS compression_locks ("
            "session_id TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at REAL NOT NULL, "
            "ttl INTEGER NOT NULL DEFAULT 300, renew_count INTEGER DEFAULT 0)"
        )
    conn.close()
    return db_path


def _make_request(session_id: str):
    messages = [HumanMessage(content="a"), AIMessage(content="b")]
    return ModelRequest(
        model=None,
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def test_sync_acquire_release_roundtrip(lock_db):
    lock = CompactionLock(db_path=str(lock_db))

    with lock.acquire_sync(_SID, timeout_s=1.0):
        held = lock._current_holder(_SID)
        assert held is not None

    # Released: the session is immediately lockable again.
    assert lock._current_holder(_SID) is None
    with lock.acquire_sync(_SID, timeout_s=1.0):
        pass


def test_concurrent_acquire_times_out(lock_db):
    lock = CompactionLock(db_path=str(lock_db))

    with lock.acquire_sync(_SID, timeout_s=1.0):
        with pytest.raises(CompactionLockError, match="compaction lock not acquired"):
            with lock.acquire_sync(_SID, timeout_s=0.3):
                pass

    # After the holder releases, the lock is available again.
    with lock.acquire_sync(_SID, timeout_s=1.0):
        pass


def test_ttl_expiry_frees_abandoned_lock(lock_db):
    lock = CompactionLock(db_path=str(lock_db), ttl_s=1)

    conn = sqlite3.connect(lock_db)
    with conn:
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, ttl) "
            "VALUES (?, 'ghost', ?, 1)",
            (_SID, __import__("time").time() - 3600),
        )
    conn.close()

    # The abandoned lock is purged on the next acquire attempt.
    with lock.acquire_sync(_SID, timeout_s=1.0):
        pass


def test_release_only_removes_own_holder_row(lock_db):
    lock = CompactionLock(db_path=str(lock_db))
    lock._try_acquire(_SID, "holder-A")
    lock._release(_SID, "holder-B")

    assert lock._current_holder(_SID) == "holder-A"
    lock._release(_SID, "holder-A")
    assert lock._current_holder(_SID) is None


@pytest.mark.asyncio
async def test_async_acquire_releases(lock_db):
    lock = CompactionLock(db_path=str(lock_db))

    async with lock.acquire(_SID, timeout_s=1.0):
        assert lock._current_holder(_SID) is not None
    assert lock._current_holder(_SID) is None


@pytest.mark.asyncio
async def test_apply_compression_fails_open_under_foreign_lock(lock_db, monkeypatch):
    sid = "p03-failopen"
    conn = sqlite3.connect(lock_db)
    with conn:
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, ttl) "
            "VALUES (?, 'foreign-instance', ?, 3600)",
            (sid, __import__("time").time()),
        )
    conn.close()

    model_calls: list[object] = []

    class _RecordingModel:
        _llm_type = "fake"

        def invoke(self, prompt, config=None):
            model_calls.append(prompt)
            return SimpleNamespace(text="summary")

        async def ainvoke(self, prompt, config=None):
            model_calls.append(prompt)
            return SimpleNamespace(text="summary")

    mw = summarization_module.Summarization(
        model=_RecordingModel(),
        trigger=[("tokens", 500)],
        keep=("messages", 10),
        main_llm_context_window=8000,
        need_update_system_prompt=False,
    )
    request = _make_request(sid)

    # Fail-open: the untouched request comes back, no LLM call was made.
    result = await mw._aapply_compression(request, sid)
    assert result is request
    assert model_calls == []
