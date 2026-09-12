"""Unit tests for the taskflow SQLite persistence layer (task_flows table).

Mirrors tests/unit/subagent/test_store_sqlite.py: the store is a module-level
function API around module constants, so tests isolate via monkeypatched
``_DB_DIR``/``_DB_PATH`` + reset once-per-process init state; the real data
directory is never touched. The concurrency regression test ports the F3
hardening pattern (busy_timeout as the FIRST statement on every connection +
once-only schema init + tolerant check-first WAL switch).
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.taskflow.config import INITIAL_REVISION, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.registry.store_sqlite import (
    FlowConflictError,
    FlowExistsError,
    FlowNotFoundError,
)


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
    flow = await store_sqlite.create_flow("flow-1", _make_state())
    assert flow["flow_id"] == "flow-1"
    assert flow["status"] == TaskFlowStatus.RUNNING.value
    assert flow["expected_revision"] == INITIAL_REVISION
    assert flow["state"] == _make_state()
    assert flow["wait"] is None
    assert flow["child_session_key"] is None

    loaded = await store_sqlite.get_flow("flow-1")
    assert loaded == flow


@pytest.mark.asyncio
async def test_create_duplicate_flow_rejected(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state())
    with pytest.raises(FlowExistsError):
        await store_sqlite.create_flow("flow-1", _make_state(description="other"))


@pytest.mark.asyncio
async def test_get_missing_flow_returns_none(isolated_db: Path):
    assert await store_sqlite.get_flow("no-such-flow") is None


@pytest.mark.asyncio
async def test_update_bumps_revision_and_persists_fields(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state())

    updated = await store_sqlite.update_flow(
        "flow-1",
        expected_revision=INITIAL_REVISION,
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
    loaded = await store_sqlite.get_flow("flow-1")
    assert loaded == updated


@pytest.mark.asyncio
async def test_update_keeps_unset_fields(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state())

    updated = await store_sqlite.update_flow(
        "flow-1",
        expected_revision=INITIAL_REVISION,
        status=TaskFlowStatus.WAITING.value,
    )

    assert updated["expected_revision"] == 2
    assert updated["status"] == TaskFlowStatus.WAITING.value
    assert updated["state"] == _make_state()  # untouched
    assert updated["wait"] is None  # untouched
    assert updated["child_session_key"] is None  # untouched


@pytest.mark.asyncio
async def test_update_can_clear_wait_json(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state())
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, wait={"reason": "awaiting child"})

    updated = await store_sqlite.update_flow("flow-1", 2, wait=None)

    assert updated["expected_revision"] == 3
    assert updated["wait"] is None


@pytest.mark.asyncio
async def test_update_conflict_error_carries_latest_revision(isolated_db: Path):
    await store_sqlite.create_flow("flow-1", _make_state())
    await store_sqlite.update_flow("flow-1", INITIAL_REVISION, status=TaskFlowStatus.WAITING.value)

    # Stale writer still holds revision 1 while the row is at revision 2.
    with pytest.raises(FlowConflictError) as excinfo:
        await store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            status=TaskFlowStatus.DONE.value,
        )

    assert excinfo.value.latest_revision == 2
    assert excinfo.value.expected_revision == 1
    # Error message carries the latest revision so callers can re-read + retry.
    assert "revision=2" in str(excinfo.value)

    # The conflicting write must not have mutated anything.
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.WAITING.value
    assert flow["expected_revision"] == 2


@pytest.mark.asyncio
async def test_update_missing_flow_raises_not_found(isolated_db: Path):
    with pytest.raises(FlowNotFoundError):
        await store_sqlite.update_flow(
            "no-such-flow", expected_revision=1, status=TaskFlowStatus.DONE.value
        )


@pytest.mark.asyncio
async def test_concurrent_conflicting_writers_exactly_one_wins(isolated_db: Path):
    """Two same-loop writers using the SAME expected_revision: exactly one
    wins, the loser gets FlowConflictError carrying the latest revision."""
    await store_sqlite.create_flow("flow-1", _make_state())

    results = await asyncio.gather(
        store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            status=TaskFlowStatus.WAITING.value,
            wait={"reason": "writer A"},
        ),
        store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
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

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["expected_revision"] == 2
    assert flow["wait"]["reason"] in ("writer A", "writer B")


@pytest.mark.asyncio
async def test_wal_journal_mode_is_enabled(isolated_db: Path):
    """The WAL switch runs during one-time schema init and sticks."""
    import aiosqlite

    await store_sqlite.create_flow("flow-1", _make_state())

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
        await store.create_flow("flow-sync", _make_state(description="sync read"))

    asyncio.run(_setup())

    flow = store.get_flow_sync("flow-sync")
    assert flow is not None
    assert flow["state"]["description"] == "sync read"
    assert flow["expected_revision"] == INITIAL_REVISION
    assert store.get_flow_sync("no-such-flow") is None


# ---------------------------------------------------------------------------
# get_active_flows_sync (LT-2): non-terminal flows for prompt auto-resume
# ---------------------------------------------------------------------------


def _create_flows(*specs: tuple[str, str]) -> None:
    """Create flows (flow_id, status) on their own event loop, sync-style."""

    async def _setup() -> None:
        for flow_id, status in specs:
            await store_sqlite.create_flow(flow_id, _make_state(flow_id), status=status)

    asyncio.run(_setup())


def test_get_active_flows_sync_returns_running(isolated_db: Path):
    _create_flows(("flow-running", TaskFlowStatus.RUNNING.value))

    active = store_sqlite.get_active_flows_sync()

    assert [flow["flow_id"] for flow in active] == ["flow-running"]
    assert active[0]["status"] == TaskFlowStatus.RUNNING.value
    assert active[0]["state"]["description"] == "flow-running"


def test_get_active_flows_sync_returns_waiting(isolated_db: Path):
    _create_flows(("flow-waiting", TaskFlowStatus.WAITING.value))

    active = store_sqlite.get_active_flows_sync()

    assert [flow["flow_id"] for flow in active] == ["flow-waiting"]
    assert active[0]["status"] == TaskFlowStatus.WAITING.value


def test_get_active_flows_sync_excludes_terminal(isolated_db: Path):
    _create_flows(
        ("flow-running", TaskFlowStatus.RUNNING.value),
        ("flow-done", TaskFlowStatus.DONE.value),
        ("flow-failed", TaskFlowStatus.FAILED.value),
        ("flow-cancelled", TaskFlowStatus.CANCELLED.value),
    )

    active = store_sqlite.get_active_flows_sync()

    assert [flow["flow_id"] for flow in active] == ["flow-running"]


def test_get_active_flows_sync_empty(isolated_db: Path):
    assert store_sqlite.get_active_flows_sync() == []


def test_get_active_flows_sync_ordered_by_rev(isolated_db: Path):
    async def _setup() -> None:
        await store_sqlite.create_flow("flow-low", _make_state("low"))
        await store_sqlite.create_flow("flow-mid", _make_state("mid"))
        await store_sqlite.update_flow("flow-mid", INITIAL_REVISION, wait={"reason": "bump"})
        await store_sqlite.create_flow("flow-high", _make_state("high"))
        for _ in range(3):
            current = await store_sqlite.get_flow("flow-high")
            assert current is not None
            await store_sqlite.update_flow(
                "flow-high", current["expected_revision"], wait={"reason": "bump"}
            )

    asyncio.run(_setup())

    active = store_sqlite.get_active_flows_sync()

    assert [flow["flow_id"] for flow in active] == ["flow-high", "flow-mid", "flow-low"]
    assert [flow["expected_revision"] for flow in active] == [4, 2, 1]


def test_get_active_flows_sync_failure_returns_empty(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing sync init/read must fail open with an empty list, not raise."""

    def _boom() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store_sqlite, "_ensure_tables_sync", _boom)

    assert store_sqlite.get_active_flows_sync() == []


def test_full_persistence_across_restart_new_event_loop(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Phase 1 writes on loop 1; phase 2 re-reads on a NEW event loop after a
    simulated process restart. Nothing may be lost (acceptance: cross-restart
    read-back of state, revision, status and child_session_key)."""

    async def phase1() -> None:
        await store_sqlite.create_flow("flow-1", _make_state(description="restart probe"))
        await store_sqlite.update_flow(
            "flow-1",
            expected_revision=INITIAL_REVISION,
            state={"description": "restart probe", "steps": [], "results": ["r1"]},
            status=TaskFlowStatus.WAITING.value,
            child_session_key="agent:main:subagent:child-9",
        )

    asyncio.run(phase1())
    _reset_init_state(monkeypatch)

    async def phase2() -> dict | None:
        return await store_sqlite.get_flow("flow-1")

    flow = asyncio.run(phase2())
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.WAITING.value
    assert flow["child_session_key"] == "agent:main:subagent:child-9"
    assert flow["expected_revision"] == 2
    assert flow["state"]["results"] == ["r1"]
    assert flow["state"]["description"] == "restart probe"
