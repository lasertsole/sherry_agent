import pytest
from future_subagent.announce.idempotency import build_idempotency_key
from future_subagent.announce.output import (
    build_child_completion_findings,
    build_compact_announce_stats_line,
)
from future_subagent.announce.dispatch import AnnounceDispatchType, resolve_dispatch_type
from future_subagent.announce.origin import resolve_announce_origin
from future_subagent.types.registry import SubagentRunRecord, RunOutcome, RunOutcomeStatus, ExecutionState, CompletionState


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
        assert "OK" in findings
        assert "All done" in findings


class TestCompactStatsLine:
    def test_mixed_outcomes(self):
        runs = [
            SubagentRunRecord(run_id="r1", child_session_key="c1", requester_session_key="p", task="t",
                              execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK))),
            SubagentRunRecord(run_id="r2", child_session_key="c2", requester_session_key="p", task="t",
                              execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.ERROR))),
            SubagentRunRecord(run_id="r3", child_session_key="c3", requester_session_key="p", task="t",
                              execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT))),
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
        run = SubagentRunRecord(run_id="r1", child_session_key="child", requester_session_key="parent", task="t", agent_id="main")
        origin = resolve_announce_origin(run)
        assert origin["child_session_key"] == "child"
        assert origin["requester_session_key"] == "parent"
        assert origin["agent_id"] == "main"
