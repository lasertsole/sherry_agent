import pytest
from agent.tools.subagent.orphan.recovery import (
    evaluate_recovery_gate,
    scan_orphaned_sessions,
    reclassify_legacy_timeout,
    finalize_interrupted_run_with_retry,
    recovery_attempts_persisted,
    _MAX_RECOVERY_ATTEMPTS,
)
from agent.tools.subagent.registry.memory import set_run, clear
from agent.tools.subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
)


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clean():
    clear()
    recovery_attempts_persisted.clear()
    from agent.tools.subagent.orphan.recovery import _recovery_tasks

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


class TestRecoveryBookkeeping:
    """Neither recovery dict may outlive its run.

    The task entry must leave ``_recovery_tasks`` on EVERY exit path (the
    early returns for a vanished/non-live run previously leaked a finished
    task), and the attempt counter is dropped once the run is gone or
    terminal — only runs that can still be scheduled again keep theirs.
    """

    @pytest.mark.asyncio
    async def test_gone_run_leaves_no_task_or_counter_entry(self):
        from agent.tools.subagent.orphan import recovery as rec

        await rec.schedule_orphan_recovery("ghost", delay_seconds=0)
        await rec._recovery_tasks["ghost"]

        assert "ghost" not in rec._recovery_tasks
        assert "ghost" not in rec.recovery_attempts_persisted

    @pytest.mark.asyncio
    async def test_terminal_run_drops_its_counter(self):
        from agent.tools.subagent.orphan import recovery as rec

        set_run(_make_run(execution=ExecutionState(status=ExecutionStatus.TERMINAL)))
        rec.recovery_attempts_persisted["r1"] = 2

        await rec.schedule_orphan_recovery("r1", delay_seconds=0)
        await rec._recovery_tasks["r1"]

        assert "r1" not in rec._recovery_tasks
        assert "r1" not in rec.recovery_attempts_persisted

    @pytest.mark.asyncio
    async def test_still_live_run_keeps_its_counter(self, monkeypatch):
        """A resumed-but-still-live run keeps the counter that gates rescheduling."""
        import time

        from agent.tools.subagent.orphan import recovery as rec

        set_run(
            _make_run(
                execution=ExecutionState(
                    status=ExecutionStatus.INTERRUPTED, started_at=time.monotonic()
                )
            )
        )

        async def _fake_resume(_run) -> bool:
            return True

        monkeypatch.setattr(rec, "_attempt_resume", _fake_resume)

        await rec.schedule_orphan_recovery("r1", delay_seconds=0)
        await rec._recovery_tasks["r1"]

        assert rec.recovery_attempts_persisted.get("r1") == 1
        assert "r1" not in rec._recovery_tasks
