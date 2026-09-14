import asyncio

import pytest
from loguru import logger

from agent.tools.subagent.announce import core as announce_core
from agent.tools.subagent.announce.dispatch import AnnounceDispatchType, resolve_dispatch_type
from agent.tools.subagent.announce.idempotency import build_idempotency_key
from agent.tools.subagent.announce.origin import resolve_announce_origin
from agent.tools.subagent.announce.output import (
    build_child_completion_findings,
    build_compact_announce_stats_line,
)
from agent.tools.subagent.types.registry import (
    SubagentRunRecord,
    RunOutcome,
    RunOutcomeStatus,
    ExecutionState,
    CompletionState,
)

pytestmark = [pytest.mark.unit]


class TestIdempotency:
    def test_build_idempotency_key(self):
        key = build_idempotency_key("r1", generation=0)
        assert "r1" in key
        assert "gen:0" in key

    def test_different_generations_different_keys(self):
        k1 = build_idempotency_key("r1", 0)
        k2 = build_idempotency_key("r1", 1)
        assert k1 != k2


class TestBuildCompletionFindings:
    def test_basic(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="child",
            requester_session_key="parent",
            task="test task",
            label="worker-1",
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK)),
            completion=CompletionState(result_text="All done"),
        )
        findings = build_child_completion_findings(run)
        assert "worker-1" in findings
        assert "ok" in findings
        assert "All done" in findings


class TestCompactStatsLine:
    def test_mixed_outcomes(self):
        runs = [
            SubagentRunRecord(
                run_id="r1",
                child_session_key="c1",
                requester_session_key="p",
                task="t",
                execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK)),
            ),
            SubagentRunRecord(
                run_id="r2",
                child_session_key="c2",
                requester_session_key="p",
                task="t",
                execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.ERROR)),
            ),
            SubagentRunRecord(
                run_id="r3",
                child_session_key="c3",
                requester_session_key="p",
                task="t",
                execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT)),
            ),
        ]
        line = build_compact_announce_stats_line(runs)
        assert "total=3" in line
        assert "ok=1" in line
        assert "errors=1" in line
        assert "timeouts=1" in line


class TestDispatch:
    def test_default_dispatch_type(self):
        assert resolve_dispatch_type(None) == AnnounceDispatchType.DIRECT


class TestOrigin:
    def test_resolve_origin(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="child",
            requester_session_key="parent",
            task="t",
            agent_id="main",
        )
        origin = resolve_announce_origin(run)
        assert origin.child_session_key == "child"
        assert origin.requester_session_key == "parent"
        assert origin.agent_id == "main"


def _wake_run() -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id="r-wake",
        child_session_key="child",
        requester_session_key="parent",
        task="t",
        wake_on_descendant_settle=True,
    )


class TestDescendantWakeBackgroundTask:
    """Audit #44: the fire-and-forget wake check must be tracked and never drop exceptions."""

    @pytest.mark.asyncio
    async def test_scheduled_check_is_tracked_and_removed_on_cancel(self):
        announce_core._schedule_descendant_wake_if_needed(_wake_run())

        assert len(announce_core._background_tasks) == 1
        task = next(iter(announce_core._background_tasks))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert announce_core._background_tasks == set()

    @pytest.mark.asyncio
    async def test_task_exception_is_retrieved_and_logged(self, monkeypatch):
        records: list[str] = []
        sink_id = logger.add(lambda message: records.append(str(message)), level="WARNING")
        real_sleep = asyncio.sleep

        async def _failing_sleep(delay: float) -> None:
            if delay == 5.0:
                raise RuntimeError("wake sleep boom")
            await real_sleep(delay)

        monkeypatch.setattr(asyncio, "sleep", _failing_sleep)
        try:
            announce_core._schedule_descendant_wake_if_needed(_wake_run())
            task = next(iter(announce_core._background_tasks))
            await asyncio.wait([task])
        finally:
            logger.remove(sink_id)

        assert any("wake sleep boom" in record for record in records)
        assert announce_core._background_tasks == set()
