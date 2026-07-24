import pytest
from future_subagent.registry.completion import should_update_run_outcome, resolve_finalized_task_state, resolve_lifecycle_outcome
from future_subagent.registry.cleanup import resolve_cleanup_completion_reason, resolve_deferred_cleanup_decision
from future_subagent.types.registry import SubagentRunRecord, RunOutcome, RunOutcomeStatus, ExecutionState, DeliveryStatus, CompletionDeliveryState
from future_subagent.types.lifecycle import LifecycleEndedReason
from future_subagent.types.spawn import SpawnMode


class TestCompletion:
    def test_should_update_from_none(self):
        assert should_update_run_outcome(None, RunOutcome(status=RunOutcomeStatus.OK))

    def test_should_not_update_from_ok_to_unknown(self):
        assert not should_update_run_outcome(
            RunOutcome(status=RunOutcomeStatus.OK),
            RunOutcome(status=RunOutcomeStatus.UNKNOWN),
        )

    def test_should_update_from_unknown_to_ok(self):
        assert should_update_run_outcome(
            RunOutcome(status=RunOutcomeStatus.UNKNOWN),
            RunOutcome(status=RunOutcomeStatus.OK),
        )

    def test_resolve_finalized_ok(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK)),
        )
        result = resolve_finalized_task_state(run)
        assert result["ended_reason"] == LifecycleEndedReason.COMPLETE
        assert result["terminal_state"] == "succeeded"

    def test_resolve_finalized_timeout(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT)),
        )
        result = resolve_finalized_task_state(run)
        assert result["ended_reason"] == LifecycleEndedReason.TIMEOUT

    def test_resolve_finalized_no_outcome(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
        )
        result = resolve_finalized_task_state(run)
        assert result["ended_reason"] == LifecycleEndedReason.ORPHANED

    def test_resolve_lifecycle_outcome(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.ERROR, error="fail")),
        )
        assert resolve_lifecycle_outcome(run) == RunOutcomeStatus.ERROR


class TestCleanup:
    def test_session_mode_no_cleanup(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            spawn_mode=SpawnMode.SESSION,
        )
        assert resolve_cleanup_completion_reason(run) is None

    def test_delivered_cleanup(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            delivery=CompletionDeliveryState(status=DeliveryStatus.DELIVERED),
        )
        assert resolve_cleanup_completion_reason(run) == "delivered"

    def test_deferred_cleanup_keep(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            cleanup="keep",
        )
        should, reason = resolve_deferred_cleanup_decision(run)
        assert not should
        assert "keep" in reason

    def test_deferred_cleanup_delivered(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            delivery=CompletionDeliveryState(status=DeliveryStatus.DELIVERED),
        )
        should, reason = resolve_deferred_cleanup_decision(run)
        assert should
