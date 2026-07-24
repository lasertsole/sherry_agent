import pytest
import asyncio
from future_subagent.orphan.recovery import (
    evaluate_recovery_gate,
    scan_orphaned_sessions,
    reclassify_legacy_timeout,
    finalize_interrupted_run_with_retry,
    recovery_attempts_persisted,
    _MAX_RECOVERY_ATTEMPTS,
)
from future_subagent.registry.memory import set_run, clear
from future_subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
)


@pytest.fixture(autouse=True)
def _clean():
    clear()
    recovery_attempts_persisted.clear()
    from future_subagent.orphan.recovery import _recovery_tasks
    for t in _recovery_tasks.values():
        if not t.done():
            t.cancel()
    _recovery_tasks.clear()
    yield
    clear()
    recovery_attempts_persisted.clear()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestEvaluateRecoveryGate:
    def test_recoverable(self):
        run = _make_run()
        assert evaluate_recovery_gate(run) == "recoverable"

    def test_wedged_by_age(self):
        import time
        old_time = time.monotonic() - 100000
        run = _make_run(
            execution=ExecutionState(started_at=old_time),
        )
        result = evaluate_recovery_gate(run)
        assert result == "wedged"

    def test_wedged_by_max_attempts(self):
        run = _make_run(recovery_attempts_persisted=_MAX_RECOVERY_ATTEMPTS + 1)
        result = evaluate_recovery_gate(run)
        assert result == "wedged"

    def test_aborted_last_run(self):
        run = _make_run(aborted_last_run=True)
        assert evaluate_recovery_gate(run) == "aborted_last_run"


class TestScanOrphanedSessions:
    @pytest.mark.asyncio
    async def test_no_orphans(self):
        orphans = await scan_orphaned_sessions()
        assert orphans == []

    @pytest.mark.asyncio
    async def test_aborted_run_is_orphan(self):
        run = _make_run(aborted_last_run=True)
        set_run(run)
        orphans = await scan_orphaned_sessions()
        assert len(orphans) == 1

    @pytest.mark.asyncio
    async def test_no_task_is_orphan(self):
        run = _make_run()
        set_run(run)
        orphans = await scan_orphaned_sessions()
        assert len(orphans) == 1

    @pytest.mark.asyncio
    async def test_terminal_not_orphan(self):
        run = _make_run(
            execution=ExecutionState(
                status=ExecutionStatus.TERMINAL,
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
        )
        set_run(run)
        orphans = await scan_orphaned_sessions()
        assert len(orphans) == 0


class TestReclassifyLegacyTimeout:
    def test_reclassify_success(self):
        run = _make_run(
            aborted_last_run=True,
            ended_reason="timeout",
            execution=ExecutionState(
                status=ExecutionStatus.TERMINAL,
                outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT),
            ),
        )
        set_run(run)
        result = reclassify_legacy_timeout(run)
        assert result is not None
        assert result.ended_reason == "interrupted"
        assert result.execution.status == ExecutionStatus.INTERRUPTED

    def test_no_aborted_last_run(self):
        run = _make_run(
            aborted_last_run=False,
            ended_reason="timeout",
            execution=ExecutionState(status=ExecutionStatus.TERMINAL),
        )
        result = reclassify_legacy_timeout(run)
        assert result is None

    def test_not_timeout_reason(self):
        run = _make_run(
            aborted_last_run=True,
            ended_reason="complete",
            execution=ExecutionState(status=ExecutionStatus.TERMINAL),
        )
        result = reclassify_legacy_timeout(run)
        assert result is None

    def test_not_terminal_status(self):
        run = _make_run(
            aborted_last_run=True,
            ended_reason="timeout",
            execution=ExecutionState(status=ExecutionStatus.RUNNING),
        )
        result = reclassify_legacy_timeout(run)
        assert result is None


class TestFinalizeInterruptedRunWithRetry:
    @pytest.mark.asyncio
    async def test_already_terminal(self):
        run = _make_run(
            execution=ExecutionState(
                status=ExecutionStatus.TERMINAL,
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
        )
        set_run(run)
        result = await finalize_interrupted_run_with_retry(run.run_id, max_attempts=1)
        assert result is not None
        assert result.execution.status == ExecutionStatus.TERMINAL

    @pytest.mark.asyncio
    async def test_nonexistent_run(self):
        result = await finalize_interrupted_run_with_retry("nonexistent", max_attempts=1)
        assert result is None
