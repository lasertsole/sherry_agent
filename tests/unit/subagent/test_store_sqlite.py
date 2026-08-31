"""Unit tests for the SQLite run-registry store (subagent_runs / settle_wake_state).

Concurrency regression tests port the F3 hardening pattern (commit 608f4f6,
PendingInjectionStore) to this module: busy_timeout as the FIRST statement on
every connection + once-per-process schema init + tolerant check-first WAL
switch. Before that pattern, every call re-opened a fresh connection with no
busy_timeout and re-ran ensure_db, so concurrent writers could hit
``sqlite3.OperationalError: database is locked`` and silently lose rows.

The store is a module-level function API around module constants, so tests
isolate via monkeypatched ``_DB_PATH``/``_DB_DIR`` + reset init state — the
real data directory is never touched.
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.subagent.registry import store_sqlite
from agent.tools.subagent.types.registry import SubagentRunRecord


def _make_run(run_id: str = "run-abc-123", task: str = "subagent task payload") -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=f"agent:main:subagent:{run_id}",
        requester_session_key="agent:main:session:sess-1",
        task=task,
    )


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the store module at a tmp_path db and reset once-per-process init state."""
    db_path = tmp_path / "subagent_registry.db"
    monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
    monkeypatch.setattr(store_sqlite, "_DB_PATH", db_path)
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    # Fresh lock per test, mirroring the reference's fresh per-instance lock:
    # a contended acquire BINDS an asyncio.Lock to the acquiring test's event
    # loop, and pytest-asyncio creates a new loop per test.
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    return db_path


@pytest.mark.asyncio
async def test_concurrent_distinct_upserts_all_persisted(isolated_db: Path):
    """F3 regression: 16 same-loop concurrent upserts (distinct run_ids) → zero row loss.

    Before the busy_timeout + once-only-init fix, per-call connections with no
    busy timeout could raise OperationalError("database is locked") under this
    exact load and rows were silently lost (upsert_run_to_sqlite has no
    try/except — an escaping OperationalError would surface to callers).
    """
    runs = [_make_run(run_id=f"run-{i:02d}", task=f"payload {i}") for i in range(16)]

    await asyncio.gather(*(store_sqlite.upsert_run_to_sqlite(run) for run in runs))

    loaded = await store_sqlite.load_runs_from_sqlite()
    assert len(loaded) == 16
    assert set(loaded) == {run.run_id for run in runs}
    for run in runs:
        record = loaded.get(run.run_id)
        assert record is not None, f"row for {run.run_id} was lost"
        assert record.task == run.task
        assert record.requester_session_key == run.requester_session_key


@pytest.mark.asyncio
async def test_concurrent_same_run_id_upserts_yield_single_row(isolated_db: Path):
    """F3 regression: same-loop concurrent upserts colliding on ONE run_id → exactly one row.

    The run_id primary key must hold under concurrency: all 16 writers succeed
    (no exceptions escape — gather re-raises) and INSERT OR REPLACE collapses
    to a single stored row (last writer wins), never duplicates or lock errors.
    """
    runs = [_make_run(run_id="run-abc-123", task=f"payload {i}") for i in range(16)]

    await asyncio.gather(*(store_sqlite.upsert_run_to_sqlite(run) for run in runs))

    loaded = await store_sqlite.load_runs_from_sqlite()
    assert len(loaded) == 1
    assert loaded["run-abc-123"].task in {run.task for run in runs}
