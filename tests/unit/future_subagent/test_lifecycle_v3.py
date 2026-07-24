import pytest
import time
from future_subagent.registry.lifecycle import (
    _should_suspend_pending_final_delivery,
    _should_retain_attachments,
    _arbitrate_kill_vs_completion,
    _mark_terminal_owner,
)
from future_subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    ExecutionStatus,
    CompletionState,
    CompletionDeliveryState,
    DeliveryStatus,
    RunOutcome,
    RunOutcomeStatus,
    KillReconciliationState,
)
from future_subagent.types.spawn import SpawnMode


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:future_subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestShouldSuspendPendingFinalDelivery:
    def test_keep_cleanup_complete_outcome_ok_expects_pending(self):
        run = _make_run(
            cleanup="keep",
            ended_reason="complete",
            expects_completion_message=True,
            execution=ExecutionState(
                status=ExecutionStatus.TERMINAL,
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
            delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
        )
        assert _should_suspend_pending_final_delivery(run) is True

    def test_delete_cleanup_no_suspend(self):
        run = _make_run(
            cleanup="delete",
            ended_reason="complete",
            expects_completion_message=True,
            execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
            delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
        )
        assert _should_suspend_pending_final_delivery(run) is False

    def test_error_outcome_no_suspend(self):
        run = _make_run(
            cleanup="keep",
            ended_reason="complete",
            expects_completion_message=True,
            execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.ERROR),
            ),
            delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
        )
        assert _should_suspend_pending_final_delivery(run) is False

    def test_not_expects_no_suspend(self):
        run = _make_run(
            cleanup="keep",
            ended_reason="complete",
            expects_completion_message=False,
            execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
            delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
        )
        assert _should_suspend_pending_final_delivery(run) is False

    def test_not_pending_no_suspend(self):
        run = _make_run(
            cleanup="keep",
            ended_reason="complete",
            expects_completion_message=True,
            execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.OK),
            ),
            delivery=CompletionDeliveryState(status=DeliveryStatus.NOT_REQUIRED),
        )
        assert _should_suspend_pending_final_delivery(run) is False

    def test_no_outcome_no_suspend(self):
        run = _make_run(
            cleanup="keep",
            ended_reason="complete",
            expects_completion_message=True,
            delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
        )
        assert _should_suspend_pending_final_delivery(run) is False


class TestShouldRetainAttachments:
    def test_retain_flag(self):
        run = _make_run(retain_attachments_on_keep=True)
        assert _should_retain_attachments(run) is True

    def test_keep_cleanup(self):
        run = _make_run(cleanup="keep")
        assert _should_retain_attachments(run) is True

    def test_session_mode(self):
        run = _make_run(spawn_mode=SpawnMode.SESSION)
        assert _should_retain_attachments(run) is True

    def test_delete_run_mode(self):
        run = _make_run(cleanup="delete", spawn_mode=SpawnMode.RUN)
        assert _should_retain_attachments(run) is False

    def test_keep_takes_precedence(self):
        run = _make_run(cleanup="keep", spawn_mode=SpawnMode.RUN)
        assert _should_retain_attachments(run) is True


class TestArbitrateKillVsCompletion:
    def test_no_kill_reconciliation(self):
        run = _make_run()
        result = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK))
        assert result.run_id == "r1"

    def test_reconciled_kill(self):
        kr = KillReconciliationState(reconciled=True)
        run = _make_run(kill_reconciliation=kr)
        result = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK))
        assert result.run_id == "r1"

    def test_provider_ok_overrides_kill(self):
        kr = KillReconciliationState(
            snapshot_execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.KILLED),
            ),
            reconciled=False,
        )
        run = _make_run(
            kill_reconciliation=kr,
            completion=CompletionState(result_text="done"),
        )
        result = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK))
        assert result.kill_reconciliation.reconciled is True
        assert result.suppress_completion_delivery is False

    def test_kill_takes_precedence_on_error(self):
        kr = KillReconciliationState(
            snapshot_execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.KILLED),
            ),
            reconciled=False,
        )
        run = _make_run(kill_reconciliation=kr)
        result = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.ERROR, error="fail"))
        assert result.kill_reconciliation.reconciled is True


class TestMarkTerminalOwner:
    def test_sets_owner(self):
        run = _make_run()
        result = _mark_terminal_owner(run, "outcome:ok")
        assert result.terminal_owner == "outcome:ok"

    def test_does_not_overwrite(self):
        run = _make_run(terminal_owner="existing")
        result = _mark_terminal_owner(run, "outcome:ok")
        assert result.terminal_owner == "existing"
