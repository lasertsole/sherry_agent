"""Behavioural tests for the subagent lifecycle arbitration and kill paths.

Both modules decide what happens when a kill and a completion race, and which
terminal owner a run records — semantics that used to live only in comments.
The records are built directly (no registry, no event loop) so each rule is
visible in one place:

* ``_arbitrate_kill_vs_completion`` — the provider's own OK completion overrides
  a kill snapshot; every other shape lets the kill win;
* ``_apply_kill_reconciliation`` / ``_mark_terminal_owner`` — idempotence: the
  first owner sticks, and reconciliation finalises exactly once;
* the three terminal predicates that decide suspension, failure notification and
  attachment retention;
* ``resolve_kill_target_state`` and ``list_killable_children`` — who is killable.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from agent.tools.subagent.control.kill import list_killable_children, resolve_kill_target_state
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.registry import memory as registry_memory
from agent.tools.subagent.registry import (
    register_task,
)
from agent.tools.subagent.registry.lifecycle import (
    _apply_kill_reconciliation,
    _arbitrate_kill_vs_completion,
    _mark_terminal_owner,
    _should_notify_failure,
    _should_retain_attachments,
    _should_suspend_pending_final_delivery,
)
from agent.tools.subagent.registry.task_refs import get_task
from agent.tools.subagent.types.registry import (
    CompletionDeliveryState,
    CompletionState,
    DeliveryStatus,
    ExecutionState,
    ExecutionStatus,
    KillReconciliationState,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.unit]


def _run(
    *,
    execution: ExecutionState | None = None,
    kill_reconciliation: KillReconciliationState | None = None,
    completion: CompletionState | None = None,
    **extra: object,
) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id="run-1",
        child_session_key="child-1",
        requester_session_key="parent-1",
        task="do the thing",
        execution=execution or ExecutionState(),
        kill_reconciliation=kill_reconciliation,
        completion=completion or CompletionState(),
        **extra,  # type: ignore[arg-type]
    )


def _kill_state(
    *,
    snapshot_status: ExecutionStatus = ExecutionStatus.RUNNING,
    snapshot_outcome: RunOutcome | None = None,
    reconciled: bool = False,
) -> KillReconciliationState:
    return KillReconciliationState(
        snapshot_execution=ExecutionState(status=snapshot_status, outcome=snapshot_outcome),
        reconciled=reconciled,
    )


# ---------------------------------------------------------------------------
# Kill ↔ completion arbitration
# ---------------------------------------------------------------------------


def test_provider_completion_overrides_a_kill_when_it_produced_a_result():
    run = _run(
        kill_reconciliation=_kill_state(
            snapshot_outcome=RunOutcome(status=RunOutcomeStatus.KILLED)
        ),
        completion=CompletionState(result_text="the work finished after all"),
    )

    arbitrated = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK))

    assert arbitrated.kill_reconciliation is not None
    assert arbitrated.kill_reconciliation.reconciled is True
    # The completion is allowed to be delivered: the provider finished the work.
    assert arbitrated.suppress_completion_delivery is False


def test_kill_wins_when_the_provider_reports_ok_without_a_result():
    run = _run(
        kill_reconciliation=_kill_state(
            snapshot_outcome=RunOutcome(status=RunOutcomeStatus.KILLED)
        ),
        completion=CompletionState(result_text=""),
    )

    arbitrated = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK))

    assert arbitrated.kill_reconciliation is not None
    assert arbitrated.kill_reconciliation.reconciled is True


def test_kill_wins_when_the_provider_reports_a_failure():
    run = _run(
        kill_reconciliation=_kill_state(
            snapshot_outcome=RunOutcome(status=RunOutcomeStatus.KILLED)
        ),
        completion=CompletionState(result_text="partial output"),
    )

    arbitrated = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.ERROR))

    assert arbitrated.kill_reconciliation is not None
    assert arbitrated.kill_reconciliation.reconciled is True


def test_arbitration_without_a_kill_snapshot_is_a_no_op():
    run = _run()

    assert _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK)) is run


# ---------------------------------------------------------------------------
# Reconciliation / terminal owner
# ---------------------------------------------------------------------------


def test_reconciliation_finalises_once_and_is_idempotent():
    pending = _run(kill_reconciliation=_kill_state())

    reconciled = _apply_kill_reconciliation(pending)
    assert reconciled.kill_reconciliation is not None
    assert reconciled.kill_reconciliation.reconciled is True
    # A copy, not the input record mutated in place.
    assert pending.kill_reconciliation.reconciled is False

    assert _apply_kill_reconciliation(reconciled) is reconciled


def test_reconciliation_of_a_run_without_a_kill_is_a_no_op():
    run = _run()

    assert _apply_kill_reconciliation(run) is run


def test_the_first_terminal_owner_sticks():
    owned = _mark_terminal_owner(_run(), "kill")

    assert owned.terminal_owner == "kill"
    # A later completion must not steal ownership from the kill.
    assert _mark_terminal_owner(owned, "completion").terminal_owner == "kill"


# ---------------------------------------------------------------------------
# Terminal predicates
# ---------------------------------------------------------------------------


def test_keep_run_with_a_pending_delivery_is_suspended():
    run = _run(
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=RunOutcomeStatus.OK)
        ),
        completion=CompletionState(expects_completion_message=True, required=True),
    )
    run = run.model_copy(
        update={
            "cleanup": "keep",
            "ended_reason": "complete",
            "delivery": CompletionDeliveryState(status=DeliveryStatus.PENDING),
        }
    )

    assert _should_suspend_pending_final_delivery(run) is True

    # Each guard alone is enough to refuse: cleanup, ended_reason, the
    # completion requirement, the OK outcome, and the PENDING delivery.
    assert (
        _should_suspend_pending_final_delivery(run.model_copy(update={"cleanup": "delete"}))
        is False
    )
    assert (
        _should_suspend_pending_final_delivery(run.model_copy(update={"ended_reason": "killed"}))
        is False
    )
    assert (
        _should_suspend_pending_final_delivery(
            run.model_copy(
                update={"delivery": CompletionDeliveryState(status=DeliveryStatus.DELIVERED)}
            )
        )
        is False
    )
    assert (
        _should_suspend_pending_final_delivery(
            run.model_copy(
                update={
                    "execution": ExecutionState(
                        status=ExecutionStatus.TERMINAL,
                        outcome=RunOutcome(status=RunOutcomeStatus.ERROR),
                    )
                }
            )
        )
        is False
    )


def test_failures_without_a_completion_gate_are_notified():
    # expects_completion_message=False: the failure never reaches the announce
    # gate, so it would end silently without this direct notice.
    failed = _run(
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=RunOutcomeStatus.ERROR)
        ),
        expects_completion_message=False,
    )

    assert _should_notify_failure(failed) is True
    # An OK run has nothing to report.
    ok = failed.model_copy(
        update={
            "execution": ExecutionState(
                status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=RunOutcomeStatus.OK)
            )
        }
    )
    assert _should_notify_failure(ok) is False
    # Already-suppressed runs stay quiet.
    assert (
        _should_notify_failure(failed.model_copy(update={"suppress_announce_reason": "muted"}))
        is False
    )
    assert (
        _should_notify_failure(failed.model_copy(update={"suppress_completion_delivery": True}))
        is False
    )
    # The gate needs BOTH halves: expecting a completion message AND requiring
    # it. With only one of them set the failure is still notified directly, so
    # a run cannot be silenced by accident.
    assert (
        _should_notify_failure(failed.model_copy(update={"expects_completion_message": True}))
        is False
    )
    assert (
        _should_notify_failure(
            failed.model_copy(
                update={
                    "expects_completion_message": True,
                    "completion": CompletionState(required=False),
                }
            )
        )
        is True
    )


def test_attachments_survive_a_keep_run_or_an_explicit_retain():
    assert _should_retain_attachments(_run()) is False
    assert _should_retain_attachments(_run(cleanup="keep")) is True
    assert _should_retain_attachments(_run(retain_attachments_on_keep=True)) is True


# ---------------------------------------------------------------------------
# Kill target state / killable children
# ---------------------------------------------------------------------------


def test_kill_target_state_tracks_the_run_phase():
    assert resolve_kill_target_state(_run()) == "killable"
    assert (
        resolve_kill_target_state(
            _run(kill_reconciliation=_kill_state(snapshot_status=ExecutionStatus.RUNNING))
        )
        == "finalizing"
    )
    assert (
        resolve_kill_target_state(_run(kill_reconciliation=_kill_state(reconciled=True)))
        == "killable"
    )
    assert (
        resolve_kill_target_state(_run(execution=ExecutionState(status=ExecutionStatus.TERMINAL)))
        == "terminal"
    )


def test_list_killable_children_offers_pending_running_and_interrupted():
    clear_registry()
    try:
        for run_id, status in (
            ("pending", ExecutionStatus.PENDING),
            ("running", ExecutionStatus.RUNNING),
            ("interrupted", ExecutionStatus.INTERRUPTED),
            ("done", ExecutionStatus.TERMINAL),
        ):
            registry_memory.set_run(
                SubagentRunRecord(
                    run_id=run_id,
                    child_session_key=f"child-{run_id}",
                    requester_session_key="parent-1",
                    task="t",
                    execution=ExecutionState(status=status),
                )
            )

        killable = list_killable_children("parent-1")

        assert sorted(r.run_id for r in killable) == ["interrupted", "pending", "running"]
        # A different requester owns nothing here.
        assert list_killable_children("someone-else") == []
    finally:
        clear_registry()


@pytest.mark.asyncio
async def test_tasks_are_registered_under_the_run_id_not_the_session_key():
    """The contract that made a session-keyed queue clear a silent no-op."""

    async def _never() -> None:
        await asyncio.sleep(30)

    clear_registry()
    task = asyncio.create_task(_never())
    try:
        register_task("run-1", task)

        assert get_task("run-1") is task
        # A child SESSION key finds nothing: only run ids are ever registered.
        assert get_task("child-1") is None
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        clear_registry()
