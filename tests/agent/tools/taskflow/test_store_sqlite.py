"""Unit tests for the taskflow SQLite persistence layer (task_flows table).

Mirrors tests/agent/tools/subagent/test_store_sqlite.py: the store is a module-level
function API around module constants, so tests isolate via monkeypatched
``_DB_DIR``/``_DB_PATH`` + reset once-per-process init state; the real data
directory is never touched. The concurrency regression test ports the F3
hardening pattern (busy_timeout as the FIRST statement on every connection +
once-only schema init + tolerant check-first WAL switch).

Session isolation is part of the store contract: every row carries the owning
``session_id`` and every read/mutation filters on it, so the isolation tests
below assert that another session's flow is indistinguishable from a missing one.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.tools.taskflow.config import INITIAL_REVISION, TABLE_NAME, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.registry.store_sqlite import (
    FlowConflictError,
    FlowExistsError,
    FlowNotFoundError,
)

_SESSION = "session-A"
_OTHER = "session-B"


def _make_state(description: str = "demo flow") -> dict:
    return {"description": description, "steps": [], "results": []}


def _reset_init_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a process restart: fresh once-per-process init state.

    The db FILE is untouched, so a subsequent open on a NEW event loop must
    read back everything persisted before the restart.
    """
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)


pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(isolated_db: Path):
    flow = await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)
    assert flow["flow_id"] == "flow-1"
    assert flow["status"] == TaskFlowStatus.RUNNING.value
    assert flow["expected_revision"] == INITIAL_REVISION
    assert flow["state"] == _make_state()
    assert flow["wait"] is None
    assert flow["child_session_key"] is None
    assert flow["session_id"] == _SESSION

    loaded = await store_sqlite.get_flow("flow-1", _SESSION)
    assert loaded == flow


@pytest.mark.asyncio
async def test_create_requires_non_empty_session(isolated_db: Path):
    with pytest.raises(ValueError, match="session_id"):
        await store_sqlite.create_flow("flow-1", _make_state(), session_id="  ")


@pytest.mark.asyncio
async def test_create_duplicate_flow_rejected(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)
    with pytest.raises(FlowExistsError):
        await store_sqlite.create_flow(
            "flow-1", _make_state(description="other"), session_id=_SESSION
        )


@pytest.mark.asyncio
async def test_get_missing_flow_returns_none(isolated_db: Path):
    assert await store_sqlite.get_flow("no-such-flow", _SESSION) is None


@pytest.mark.asyncio
async def test_update_bumps_revision_and_persists_fields(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)

    updated = await store_sqlite.update_flow(
        "flow-1",
        expected_revision=INITIAL_REVISION,
        session_id=_SESSION,
        state={"description": "demo flow", "steps": [{"task": "t1"}], "results": []},
        wait={"reason": "awaiting child"},
        status=TaskFlowStatus.WAITING.value,
        child_session_key="agent:main:subagent:child-1",
    )

    assert updated["expected_revision"] == INITIAL_REVISION + 1
    assert updated["status"] == TaskFlowStatus.WAITING.value
    assert updated["wait"] == {"reason": "awaiting child"}
    assert updated["child_session_key"] == "agent:main:subagent:child-1"
    assert updated["state"]["steps"] == [{"task": "t1"}]

    # Persisted to disk, not just returned: a fresh connection reads it back.
    loaded = await store_sqlite.get_flow("flow-1", _SESSION)
    assert loaded == updated


@pytest.mark.asyncio
async def test_update_keeps_unset_fields(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)

    updated = await store_sqlite.update_flow(
        "flow-1",
        expected_revision=INITIAL_REVISION,
        session_id=_SESSION,
        status=TaskFlowStatus.WAITING.value,
    )

    assert updated["expected_revision"] == 2
    assert updated["status"] == TaskFlowStatus.WAITING.value
    assert updated["state"] == _make_state()  # untouched
    assert updated["wait"] is None  # untouched
    assert updated["child_session_key"] is None  # untouched


@pytest.mark.asyncio
async def test_update_can_clear_wait_json(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)
    await store_sqlite.update_flow(
        "flow-1", INITIAL_REVISION, session_id=_SESSION, wait={"reason": "awaiting child"}
    )

    updated = await store_sqlite.update_flow("flow-1", 2, session_id=_SESSION, wait=None)

    assert updated["expected_revision"] == 3
    assert updated["wait"] is None


@pytest.mark.asyncio
async def test_update_conflict_error_carries_latest_revision(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)
    await store_sqlite.update_flow(
        "flow-1", INITIAL_REVISION, session_id=_SESSION, status=TaskFlowStatus.WAITING.value
    )

    # Stale writer still holds revision 1 while the row is at revision 2.
    with pytest.raises(FlowConflictError) as excinfo:
        await store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            session_id=_SESSION,
            status=TaskFlowStatus.DONE.value,
        )

    assert excinfo.value.latest_revision == 2
    assert excinfo.value.expected_revision == 1
    # Error message carries the latest revision so callers can re-read + retry.
    assert "revision=2" in str(excinfo.value)

    # The conflicting write must not have mutated anything.
    flow = await store_sqlite.get_flow("flow-1", _SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.WAITING.value
    assert flow["expected_revision"] == 2


@pytest.mark.asyncio
async def test_update_missing_flow_raises_not_found(isolated_db: Path):
    with pytest.raises(FlowNotFoundError):
        await store_sqlite.update_flow(
            "no-such-flow",
            expected_revision=1,
            session_id=_SESSION,
            status=TaskFlowStatus.DONE.value,
        )


@pytest.mark.asyncio
async def test_concurrent_conflicting_writers_exactly_one_wins(isolated_db: Path):
    """Two same-loop writers using the SAME expected_revision: exactly one
    wins, the loser gets FlowConflictError carrying the latest revision."""
    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)

    results = await asyncio.gather(
        store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            session_id=_SESSION,
            status=TaskFlowStatus.WAITING.value,
            wait={"reason": "writer A"},
        ),
        store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            session_id=_SESSION,
            status=TaskFlowStatus.WAITING.value,
            wait={"reason": "writer B"},
        ),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1, f"expected exactly one winner, got {results!r}"
    assert len(errors) == 1
    assert isinstance(errors[0], FlowConflictError)
    assert errors[0].latest_revision == 2

    flow = await store_sqlite.get_flow("flow-1", _SESSION)
    assert flow is not None
    assert flow["expected_revision"] == 2
    assert flow["wait"]["reason"] in ("writer A", "writer B")


@pytest.mark.asyncio
async def test_wal_journal_mode_is_enabled(isolated_db: Path):
    """The WAL switch runs during one-time schema init and sticks."""
    import aiosqlite

    await store_sqlite.create_flow("flow-1", _make_state(), session_id=_SESSION)

    async with aiosqlite.connect(isolated_db) as db:
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"


def test_get_flow_sync_without_event_loop(isolated_db: Path):
    """The sync (stdlib sqlite3) path works with no running event loop.

    Exercises the threading.Lock-guarded one-time sync table creation
    (mirroring store_sqlite.py:60-70 / 165-186 of the subagent blueprint).
    """
    from agent.tools.taskflow.registry import store_sqlite as store

    async def _setup() -> None:
        await store.create_flow(
            "flow-sync", _make_state(description="sync read"), session_id=_SESSION
        )

    asyncio.run(_setup())

    flow = store.get_flow_sync("flow-sync", _SESSION)
    assert flow is not None
    assert flow["state"]["description"] == "sync read"
    assert flow["expected_revision"] == INITIAL_REVISION
    assert store.get_flow_sync("no-such-flow", _SESSION) is None
    assert store.get_flow_sync("flow-sync", _OTHER) is None


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_flow_is_session_scoped(isolated_db: Path):
    """Session B cannot read Session A's flow; A reads its own."""
    await store_sqlite.create_flow("flow-A", _make_state(), session_id=_SESSION)

    assert await store_sqlite.get_flow("flow-A", _OTHER) is None
    assert await store_sqlite.get_flow("flow-A", _SESSION) is not None


@pytest.mark.asyncio
async def test_update_flow_is_session_scoped(isolated_db: Path):
    """A cross-session writer gets FlowNotFoundError and mutates nothing."""
    flow = await store_sqlite.create_flow("flow-A", _make_state(), session_id=_SESSION)

    with pytest.raises(FlowNotFoundError):
        await store_sqlite.update_flow(
            "flow-A",
            expected_revision=flow["expected_revision"],
            session_id=_OTHER,
            status=TaskFlowStatus.DONE.value,
        )

    still = await store_sqlite.get_flow("flow-A", _SESSION)
    assert still is not None
    assert still["status"] == TaskFlowStatus.RUNNING.value
    assert still["expected_revision"] == INITIAL_REVISION


# ---------------------------------------------------------------------------
# get_active_flows_sync: non-terminal flows for prompt auto-resume
# ---------------------------------------------------------------------------


def _create_flows(*specs: tuple[str, str], session_id: str = _SESSION) -> None:
    """Create flows (flow_id, status) on their own event loop, sync-style."""

    async def _setup() -> None:
        for flow_id, status in specs:
            await store_sqlite.create_flow(
                flow_id, _make_state(flow_id), session_id=session_id, status=status
            )

    asyncio.run(_setup())


def test_get_active_flows_sync_returns_running(isolated_db: Path):
    _create_flows(("flow-running", TaskFlowStatus.RUNNING.value))

    active = store_sqlite.get_active_flows_sync(_SESSION)

    assert [flow["flow_id"] for flow in active] == ["flow-running"]
    assert active[0]["status"] == TaskFlowStatus.RUNNING.value
    assert active[0]["state"]["description"] == "flow-running"


def test_get_active_flows_sync_returns_waiting(isolated_db: Path):
    _create_flows(("flow-waiting", TaskFlowStatus.WAITING.value))

    active = store_sqlite.get_active_flows_sync(_SESSION)

    assert [flow["flow_id"] for flow in active] == ["flow-waiting"]
    assert active[0]["status"] == TaskFlowStatus.WAITING.value


def test_get_active_flows_sync_excludes_terminal(isolated_db: Path):
    _create_flows(
        ("flow-running", TaskFlowStatus.RUNNING.value),
        ("flow-done", TaskFlowStatus.DONE.value),
        ("flow-failed", TaskFlowStatus.FAILED.value),
        ("flow-cancelled", TaskFlowStatus.CANCELLED.value),
    )

    active = store_sqlite.get_active_flows_sync(_SESSION)

    assert [flow["flow_id"] for flow in active] == ["flow-running"]


def test_get_active_flows_sync_empty(isolated_db: Path):
    assert store_sqlite.get_active_flows_sync(_SESSION) == []


def test_get_active_flows_sync_is_session_scoped(isolated_db: Path):
    _create_flows(("flow-A", TaskFlowStatus.RUNNING.value), session_id=_SESSION)
    _create_flows(("flow-B", TaskFlowStatus.RUNNING.value), session_id=_OTHER)

    assert [flow["flow_id"] for flow in store_sqlite.get_active_flows_sync(_SESSION)] == ["flow-A"]
    assert [flow["flow_id"] for flow in store_sqlite.get_active_flows_sync(_OTHER)] == ["flow-B"]


def test_get_active_flows_sync_ordered_by_rev(isolated_db: Path):
    async def _setup() -> None:
        await store_sqlite.create_flow("flow-low", _make_state("low"), session_id=_SESSION)
        await store_sqlite.create_flow("flow-mid", _make_state("mid"), session_id=_SESSION)
        await store_sqlite.update_flow(
            "flow-mid", INITIAL_REVISION, session_id=_SESSION, wait={"reason": "bump"}
        )
        await store_sqlite.create_flow("flow-high", _make_state("high"), session_id=_SESSION)
        for _ in range(3):
            current = await store_sqlite.get_flow("flow-high", _SESSION)
            assert current is not None
            await store_sqlite.update_flow(
                "flow-high",
                current["expected_revision"],
                session_id=_SESSION,
                wait={"reason": "bump"},
            )

    asyncio.run(_setup())

    active = store_sqlite.get_active_flows_sync(_SESSION)

    assert [flow["flow_id"] for flow in active] == ["flow-high", "flow-mid", "flow-low"]
    assert [flow["expected_revision"] for flow in active] == [4, 2, 1]


def test_get_active_flows_sync_failure_returns_empty(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing sync init/read must fail open with an empty list, not raise."""

    def _boom() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store_sqlite, "_ensure_tables_sync", _boom)

    assert store_sqlite.get_active_flows_sync(_SESSION) == []


# ---------------------------------------------------------------------------
# get_all_flows_sync: the session board
# ---------------------------------------------------------------------------


def test_get_all_flows_sync_filters_are_session_scoped(isolated_db: Path):
    async def _setup() -> None:
        await store_sqlite.create_flow("flow-run", _make_state("running"), session_id=_SESSION)
        await store_sqlite.create_flow(
            "flow-done", _make_state("done"), session_id=_SESSION, status=TaskFlowStatus.DONE.value
        )
        await store_sqlite.create_flow("flow-other", _make_state("other"), session_id=_OTHER)

    asyncio.run(_setup())

    assert [f["flow_id"] for f in store_sqlite.get_all_flows_sync(_SESSION, "active")] == [
        "flow-run"
    ]
    assert [f["flow_id"] for f in store_sqlite.get_all_flows_sync(_SESSION, "all")] == [
        "flow-done",
        "flow-run",
    ]
    assert [f["flow_id"] for f in store_sqlite.get_all_flows_sync(_SESSION, "done")] == [
        "flow-done"
    ]
    assert store_sqlite.get_all_flows_sync(_OTHER, "done") == []


def test_get_all_flows_sync_failure_returns_empty(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    def _boom() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store_sqlite, "_ensure_tables_sync", _boom)

    assert store_sqlite.get_all_flows_sync(_SESSION, "done") == []


# ---------------------------------------------------------------------------
# Additive migration + session purge
# ---------------------------------------------------------------------------

_LEGACY_CREATE_TABLE_SQL = f"""
CREATE TABLE {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL
);
"""


def _create_pre_session_db(db_path: Path) -> None:
    """A database created before session isolation: no session_id column."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_LEGACY_CREATE_TABLE_SQL)
        conn.execute(
            f"INSERT INTO {TABLE_NAME} "
            "(flow_id, state_json, wait_json, expected_revision, status, child_session_key) "
            "VALUES ('flow-legacy', '{}', NULL, 1, ?, NULL)",
            (TaskFlowStatus.RUNNING.value,),
        )
        conn.commit()
    finally:
        conn.close()


def _table_columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
    finally:
        conn.close()


def test_old_db_gains_session_column_and_keeps_rows(isolated_db: Path):
    """The additive migration adds session_id; legacy rows stay readable."""
    _create_pre_session_db(isolated_db)
    assert "session_id" not in _table_columns(isolated_db)

    # Any store call triggers the sync migration path.
    legacy = store_sqlite.get_flow_sync("flow-legacy", "")

    assert legacy is not None
    assert legacy["session_id"] == ""
    assert "session_id" in _table_columns(isolated_db)
    # Legacy rows are invisible to every real session.
    assert store_sqlite.get_flow_sync("flow-legacy", _SESSION) is None


@pytest.mark.asyncio
async def test_old_db_gains_session_column_via_async_init(isolated_db: Path):
    _create_pre_session_db(isolated_db)

    await store_sqlite.ensure_db()

    assert "session_id" in _table_columns(isolated_db)
    assert await store_sqlite.get_flow("flow-legacy", _SESSION) is None
    assert await store_sqlite.get_flow("flow-legacy", "") is not None


@pytest.mark.asyncio
async def test_delete_flows_by_session_deletes_only_that_session(isolated_db: Path):
    await store_sqlite.create_flow("flow-A", _make_state(), session_id=_SESSION)
    await store_sqlite.create_flow("flow-B", _make_state(), session_id=_OTHER)

    deleted = await store_sqlite.delete_flows_by_session(_SESSION)

    assert deleted == 1
    assert await store_sqlite.get_flow("flow-A", _SESSION) is None
    assert await store_sqlite.get_flow("flow-B", _OTHER) is not None


@pytest.mark.asyncio
async def test_delete_flows_by_session_rejects_blank_session(isolated_db: Path):
    with pytest.raises(ValueError, match="session_id"):
        await store_sqlite.delete_flows_by_session("  ")


def test_full_persistence_across_restart_new_event_loop(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Phase 1 writes on loop 1; phase 2 re-reads on a NEW event loop after a
    simulated process restart. Nothing may be lost (acceptance: cross-restart
    read-back of state, revision, status and child_session_key)."""

    async def phase1() -> None:
        await store_sqlite.create_flow(
            "flow-1", _make_state(description="restart probe"), session_id=_SESSION
        )
        await store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            session_id=_SESSION,
            state={"description": "restart probe", "steps": [], "results": ["r1"]},
            status=TaskFlowStatus.WAITING.value,
            child_session_key="agent:main:subagent:child-9",
        )

    asyncio.run(phase1())
    _reset_init_state(monkeypatch)

    async def phase2() -> dict | None:
        return await store_sqlite.get_flow("flow-1", _SESSION)

    flow = asyncio.run(phase2())
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.WAITING.value
    assert flow["child_session_key"] == "agent:main:subagent:child-9"
    assert flow["expected_revision"] == 2
    assert flow["state"]["results"] == ["r1"]
    assert flow["state"]["description"] == "restart probe"
    assert flow["session_id"] == _SESSION
