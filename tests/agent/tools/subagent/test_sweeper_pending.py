"""Sweeper / orphan recovery behavior for PENDING runs (lost lane task, no resumable context)."""

import asyncio
import contextlib

import pytest

from agent.tools.subagent.orphan import recovery
from agent.tools.subagent.orphan.recovery import (
    _recovery_tasks,
    evaluate_recovery_gate,
    recovery_attempts_persisted,
    scan_orphaned_sessions,
)
from agent.tools.subagent.registry import (
    clear as clear_registry,
    get_run,
    register_run,
    register_task,
    remove_task,
)
from agent.tools.subagent.registry.lifecycle import recover_orphaned_runs
from agent.tools.subagent.types.registry import (
    ExecutionState,
    ExecutionStatus,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    recovery_attempts_persisted.clear()
    for task in _recovery_tasks.values():
        if not task.done():
            task.cancel()
    _recovery_tasks.clear()
    yield
    clear_registry()
    recovery_attempts_persisted.clear()
    _recovery_tasks.clear()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:subagent:pending_sweep",
        requester_session_key="agent:main:session:p1",
        task="test",
        execution=ExecutionState(status=ExecutionStatus.PENDING, started_at=None),
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestRecoveryGatePending:
    def test_pending_is_wedged(self):
        assert evaluate_recovery_gate(_make_run()) == "wedged"

    def test_running_without_started_at_still_recoverable(self):
        run = _make_run(execution=ExecutionState(status=ExecutionStatus.RUNNING, started_at=None))
        assert evaluate_recovery_gate(run) == "recoverable"


class TestRecoveryLoopPending:
    @pytest.mark.asyncio
    async def test_pending_orphan_is_finalized_directly(self, monkeypatch: pytest.MonkeyPatch):
        from unittest.mock import AsyncMock

        announce = AsyncMock()
        monkeypatch.setattr(recovery, "run_subagent_announce_flow", announce)

        run = register_run(
            child_session_key="agent:main:subagent:pending_orphan",
            requester_session_key="agent:main:session:p1",
            task="lost task",
            depth=1,
        )
        assert run.execution.status == ExecutionStatus.PENDING

        await recovery._recovery_loop(run.run_id, 0)

        updated = get_run(run.run_id)
        assert updated is not None
        assert updated.execution.status == ExecutionStatus.TERMINAL
        assert updated.execution.started_at is None
        assert updated.execution.ended_at is not None
        assert updated.execution.outcome is not None
        assert updated.execution.outcome.status == RunOutcomeStatus.TIMEOUT
        assert updated.execution.outcome.error == "pending orphaned"
        assert updated.ended_reason == "pending_orphaned"
        announce.assert_awaited_once()
        assert run.run_id not in _recovery_tasks


class TestScanOrphanedPending:
    @pytest.mark.asyncio
    async def test_pending_without_task_is_orphan(self):
        run = register_run(
            child_session_key="agent:main:subagent:pending_no_task",
            requester_session_key="agent:main:session:p1",
            task="no task",
            depth=1,
        )
        orphans = await scan_orphaned_sessions()
        assert [o.run_id for o in orphans] == [run.run_id]

    @pytest.mark.asyncio
    async def test_pending_with_live_task_is_not_orphan(self):
        run = register_run(
            child_session_key="agent:main:subagent:pending_live",
            requester_session_key="agent:main:session:p1",
            task="queued in lane",
            depth=1,
        )
        task = asyncio.create_task(asyncio.sleep(30))
        register_task(run.run_id, task)
        try:
            orphans = await scan_orphaned_sessions()
            assert run.run_id not in [o.run_id for o in orphans]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            remove_task(run.run_id)

    @pytest.mark.asyncio
    async def test_recover_orphaned_runs_leaves_pending_alone(self):
        run = register_run(
            child_session_key="agent:main:subagent:pending_recover",
            requester_session_key="agent:main:session:p1",
            task="pending",
            depth=1,
        )
        count = await recover_orphaned_runs()
        assert count == 0
        assert get_run(run.run_id).execution.status == ExecutionStatus.PENDING
