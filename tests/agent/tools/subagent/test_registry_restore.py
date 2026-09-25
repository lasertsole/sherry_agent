"""Startup restore behavior: leftover PENDING runs must be finalized silently at restore time.

A PENDING run restored from SQLite can never be promoted (its lane-waiting task
died with the previous process). Finalizing it at restore keeps it out of the
sweeper's orphan recovery, whose per-run announce flow previously flooded the
backend console with "Error: pending orphaned" after every restart.
"""

from unittest.mock import AsyncMock

import pytest

from agent.tools.subagent.announce import core as announce_core
from agent.tools.subagent.orphan.recovery import scan_orphaned_sessions
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.registry import get_run
from agent.tools.subagent.registry import state as registry_state
from agent.tools.subagent.registry import store_sqlite
from agent.tools.subagent.types.registry import (
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.integration]


def _make_run(run_id: str, status: ExecutionStatus, ended_reason: str | None = None):
    execution = ExecutionState(status=status, started_at=None)
    if status == ExecutionStatus.TERMINAL:
        execution = ExecutionState(
            status=status,
            started_at=None,
            ended_at=1.0,
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
        )
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=f"agent:main:subagent:{run_id}",
        requester_session_key="agent:main:session:restore_test",
        task="restore leftover",
        execution=execution,
        ended_reason=ended_reason,
    )


@pytest.fixture
def isolated_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the registry store at a tmp SQLite file and reset init state."""
    monkeypatch.setattr(store_sqlite, "_DB_PATH", tmp_path / "subagent_registry.db")
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    monkeypatch.setattr(registry_state, "_restore_done", False)
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
async def test_restore_finalizes_leftover_pending_runs_silently(isolated_store, monkeypatch):
    announce = AsyncMock()
    monkeypatch.setattr(announce_core, "run_subagent_announce_flow", announce)

    pending_run = _make_run("restore-pending-1", ExecutionStatus.PENDING)
    terminal_run = _make_run(
        "restore-terminal-1", ExecutionStatus.TERMINAL, ended_reason="complete"
    )
    store_sqlite.upsert_run_sync(pending_run)
    store_sqlite.upsert_run_sync(terminal_run)

    await registry_state.restore_runs_from_disk()

    restored_pending = get_run("restore-pending-1")
    assert restored_pending is not None
    assert restored_pending.execution.status == ExecutionStatus.TERMINAL
    assert restored_pending.execution.started_at is None
    assert restored_pending.execution.ended_at is not None
    assert restored_pending.execution.outcome is not None
    assert restored_pending.execution.outcome.status == RunOutcomeStatus.TIMEOUT
    assert restored_pending.execution.outcome.error == "pending orphaned"
    assert restored_pending.ended_reason == "pending_orphaned"

    # The previously-terminal run passes through untouched.
    restored_terminal = get_run("restore-terminal-1")
    assert restored_terminal.execution.status == ExecutionStatus.TERMINAL
    assert restored_terminal.ended_reason == "complete"

    # Restore never announces: the parent session belongs to the previous
    # process lifetime, so the deliver would only suspend.
    announce.assert_not_awaited()

    # The sweeper's orphan scan must find no live leftover runs.
    assert await scan_orphaned_sessions() == []


@pytest.mark.asyncio
async def test_restore_runs_only_once(isolated_store):
    first = _make_run("restore-once-1", ExecutionStatus.PENDING)
    store_sqlite.upsert_run_sync(first)

    await registry_state.restore_runs_from_disk()
    assert get_run("restore-once-1").ended_reason == "pending_orphaned"

    # A second call is a no-op (restore executes only once per process): a run
    # freshly spawned after restore stays PENDING.
    from agent.tools.subagent.registry import register_run

    fresh = register_run(
        child_session_key="agent:main:subagent:restore-fresh",
        requester_session_key="agent:main:session:restore_test",
        task="fresh spawn",
        depth=1,
    )
    await registry_state.restore_runs_from_disk()
    assert get_run(fresh.run_id).execution.status == ExecutionStatus.PENDING
