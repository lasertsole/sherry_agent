"""Integration tests for robustness-plan-v3 features across phases.

Tests end-to-end flows that span multiple modules:
- Swarm collect → spawn → complete → announce
- Thread binding → spawn → cleanup
- Kill arbitration → lifecycle → announce suppression
- Terminal generation guard → lifecycle → settle-wake
- Delivery dual-path routing
- Orphan recovery → reclassify → finalize
- Progress hooks full lifecycle
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from future_subagent.types.swarm import SwarmGroupConfig, SwarmRunState
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
    ThreadBindingInfo,
)
from future_subagent.types.spawn import SpawnMode
from future_subagent.types.capability import SubagentSessionRole
from future_subagent.registry.memory import set_run, clear
from future_subagent.registry.terminal_gen import TerminalGenerationTracker
from future_subagent.registry.settle_wake import RequesterSettleWakeBatch, SettleWakeState
from future_subagent.registry.work_admission import set_draining, _root_work_tasks
from future_subagent.swarm.collector import (
    configure_swarm_group,
    reserve_swarm_run,
    activate_swarm_run,
    complete_swarm_run,
    build_structured_output_prompt,
)
from future_subagent.swarm.fifo import SwarmFifoQueue
from future_subagent.spawn.thread_binding import (
    bind_thread_for_subagent_spawn,
    resolve_thread_binding_policy,
    ThreadBindingConfig,
)
from future_subagent.spawn.runtime_isolation import resolve_runtime_isolation, validate_runtime_isolation
from future_subagent.spawn.origin_routing import resolve_requester_origin_for_child
from future_subagent.spawn.gateway_dispatch import resolve_least_privilege_scopes
from future_subagent.control.kill import resolve_kill_target_state
from future_subagent.control.list import is_subagent_run_visible_to_session
from future_subagent.announce.idempotency import build_idempotency_key
from future_subagent.announce.output import build_child_completion_findings
from future_subagent.registry.lifecycle import (
    _should_suspend_pending_final_delivery,
    _should_retain_attachments,
    _arbitrate_kill_vs_completion,
)
from future_subagent.orphan.recovery import evaluate_recovery_gate, reclassify_legacy_timeout
from future_subagent.hooks.progress import (
    fire_spawned_hook,
    fire_ended_hook,
)


@pytest.fixture(autouse=True)
def _clean():
    clear()
    from future_subagent.swarm import collector as _collector
    _collector._group_configs.clear()
    from future_subagent.swarm.fifo import get_fifo
    get_fifo()._queues.clear()
    set_draining(False)
    _root_work_tasks.clear()
    from future_subagent.hooks import progress as _progress
    _progress._spawned_hooks.clear()
    _progress._progress_hooks.clear()
    _progress._ended_hooks.clear()
    yield
    clear()
    _collector._group_configs.clear()
    get_fifo()._queues.clear()
    set_draining(False)
    for t in list(_root_work_tasks):
        if not t.done():
            t.cancel()
    _root_work_tasks.clear()
    _progress._spawned_hooks.clear()
    _progress._progress_hooks.clear()
    _progress._ended_hooks.clear()


class TestSwarmCollectFullFlow:
    """Phase 1: Full swarm collect → activate → complete flow."""

    @pytest.mark.asyncio
    async def test_swarm_lifecycle(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=2))
        run1 = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        assert run1 is not None
        assert run1.swarm_run_state == SwarmRunState.RESERVED.value

        activated = await activate_swarm_run(run1.run_id)
        assert activated.swarm_run_state == SwarmRunState.ACTIVE.value

        completed = await complete_swarm_run(
            run1.run_id, RunOutcome(status=RunOutcomeStatus.OK), "done"
        )
        assert completed.swarm_run_state == SwarmRunState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_swarm_fifo_scheduling(self):
        configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=1))
        run1 = await reserve_swarm_run("g1", "task1", "agent:main:session:p1")
        run2 = await reserve_swarm_run("g1", "task2", "agent:main:session:p1")

        await activate_swarm_run(run1.run_id)
        r2 = await activate_swarm_run(run2.run_id)
        assert r2.swarm_run_state == SwarmRunState.RESERVED.value

        await complete_swarm_run(run1.run_id, RunOutcome(status=RunOutcomeStatus.OK))
        from future_subagent.registry import get_run
        r2_after = get_run(run2.run_id)
        assert r2_after.swarm_run_state == SwarmRunState.ACTIVE.value

    @pytest.mark.asyncio
    async def test_structured_output_prompt_integration(self):
        schema = {"type": "object", "properties": {"result": {"type": "string"}}}
        prompt = build_structured_output_prompt(schema)
        assert "JSON schema" in prompt
        assert "result" in prompt


class TestThreadBindingSpawnIntegration:
    """Phase 2: Thread binding → spawn policy → info propagation."""

    def test_session_mode_creates_binding(self):
        result = resolve_thread_binding_policy(
            agent_id="main",
            spawn_mode=SpawnMode.SESSION,
            child_session_key="agent:main:future_subagent:child1",
        )
        assert result.bound is True
        assert result.binding_info is not None
        assert result.binding_info.delivery_origin == "agent:main:future_subagent:child1"

    def test_binding_info_stored_in_record(self):
        result = bind_thread_for_subagent_spawn("agent:main:future_subagent:child1")
        info = result.binding_info
        from future_subagent.types.registry import ThreadBindingInfo as RegistryThreadBindingInfo
        registry_info = RegistryThreadBindingInfo(
            thread_id=info.thread_id,
            bound_at=info.bound_at,
            idle_timeout_ms=info.idle_timeout_ms,
            delivery_origin=info.delivery_origin,
        )
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="agent:main:future_subagent:child1",
            requester_session_key="agent:main:session:p1",
            task="test",
            thread_binding_info=registry_info,
        )
        assert run.thread_binding_info is not None
        assert run.thread_binding_info.thread_id == info.thread_id

    def test_run_mode_no_binding(self):
        result = resolve_thread_binding_policy(
            agent_id="main",
            spawn_mode=SpawnMode.RUN,
            child_session_key="agent:main:future_subagent:child1",
        )
        assert result.bound is False
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="agent:main:future_subagent:child1",
            requester_session_key="agent:main:session:p1",
            task="test",
            thread_binding_info=None,
        )
        assert run.thread_binding_info is None


class TestGenerationGuardLifecycle:
    """Phase 3: Terminal generation guard + kill arbitration + settle-wake."""

    def test_generation_guard_blocks_stale_callback(self):
        tracker = TerminalGenerationTracker()
        tracker.register_expected("r1", 5)
        assert tracker.is_callback_current("r1", 3) is False
        assert tracker.is_callback_current("r1", 5) is True

    def test_kill_vs_completion_provider_wins(self):
        kr = KillReconciliationState(
            snapshot_execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.KILLED),
            ),
            reconciled=False,
        )
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="child",
            requester_session_key="parent",
            task="test",
            kill_reconciliation=kr,
            completion=CompletionState(result_text="done"),
        )
        result = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.OK))
        assert result.kill_reconciliation.reconciled is True
        assert result.suppress_completion_delivery is False

    def test_kill_vs_completion_kill_wins(self):
        kr = KillReconciliationState(
            snapshot_execution=ExecutionState(
                outcome=RunOutcome(status=RunOutcomeStatus.KILLED),
            ),
            reconciled=False,
        )
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="child",
            requester_session_key="parent",
            task="test",
            kill_reconciliation=kr,
        )
        result = _arbitrate_kill_vs_completion(run, RunOutcome(status=RunOutcomeStatus.ERROR))
        assert result.kill_reconciliation.reconciled is True

    def test_settle_wake_batch_lifecycle(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        assert batch._state["parent1"] == SettleWakeState.IDLE

        batch.transition_batch("parent1", "child_completed")
        assert batch._state["parent1"] == SettleWakeState.COMPLETING

        batch.transition_batch("parent1", "all_settled")
        assert batch._state["parent1"] == SettleWakeState.SETTLED

        batch.transition_batch("parent1", "woke")
        assert batch._state["parent1"] == SettleWakeState.DONE

        batch.retire_after_settle("parent1")
        assert "parent1" not in batch._state

    def test_suspend_delivery_conditions(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="child",
            requester_session_key="parent",
            task="test",
            cleanup="keep",
            ended_reason="complete",
            expects_completion_message=True,
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK)),
            delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
        )
        assert _should_suspend_pending_final_delivery(run) is True

        run2 = run.model_copy(update={"cleanup": "delete"})
        assert _should_suspend_pending_final_delivery(run2) is False

    def test_retain_attachments_integration(self):
        run_keep = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test", cleanup="keep",
        )
        assert _should_retain_attachments(run_keep) is True

        run_session = SubagentRunRecord(
            run_id="r2", child_session_key="child", requester_session_key="parent",
            task="test", spawn_mode=SpawnMode.SESSION, cleanup="delete",
        )
        assert _should_retain_attachments(run_session) is True

        run_delete = SubagentRunRecord(
            run_id="r3", child_session_key="child", requester_session_key="parent",
            task="test", cleanup="delete", spawn_mode=SpawnMode.RUN,
        )
        assert _should_retain_attachments(run_delete) is False


class TestDeliveryDualPathIntegration:
    """Phase 4: Sub→sub internal injection vs sub→user completion message."""

    def test_idempotency_with_suffix(self):
        k1 = build_idempotency_key("r1", 0)
        k2 = build_idempotency_key("r1", 0, suffix="wake")
        assert k1 != k2

    def test_sub_to_sub_findings_format(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="agent:main:future_subagent:child",
            requester_session_key="agent:main:future_subagent:parent",
            task="build",
            label="builder",
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK)),
            completion=CompletionState(result_text="Built successfully"),
        )
        findings = build_child_completion_findings(run)
        assert "builder" in findings
        assert "OK" in findings

    def test_sub_to_user_findings_format(self):
        run = SubagentRunRecord(
            run_id="r1",
            child_session_key="agent:main:future_subagent:child",
            requester_session_key="agent:main:session:user1",
            task="build",
            label="builder",
            execution=ExecutionState(outcome=RunOutcome(status=RunOutcomeStatus.OK)),
            completion=CompletionState(result_text="Built successfully"),
        )
        findings = build_child_completion_findings(run)
        assert "builder" in findings


class TestControlPrecisionIntegration:
    """Phase 5: Kill target-state + visibility."""

    def test_kill_target_state_flow(self):
        running_run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test", execution=ExecutionState(status=ExecutionStatus.RUNNING),
        )
        assert resolve_kill_target_state(running_run) == "killable"

        terminal_run = SubagentRunRecord(
            run_id="r2", child_session_key="child", requester_session_key="parent",
            task="test",
            execution=ExecutionState(status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=RunOutcomeStatus.OK)),
        )
        assert resolve_kill_target_state(terminal_run) == "terminal"

        kr = KillReconciliationState(reconciled=False)
        finalizing_run = SubagentRunRecord(
            run_id="r3", child_session_key="child", requester_session_key="parent",
            task="test",
            execution=ExecutionState(status=ExecutionStatus.RUNNING),
            kill_reconciliation=kr,
        )
        assert resolve_kill_target_state(finalizing_run) == "finalizing"

    def test_visibility_filtering(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test", controller_session_key="controller1",
        )
        assert is_subagent_run_visible_to_session(run, "controller1") is True
        assert is_subagent_run_visible_to_session(run, "parent") is True
        assert is_subagent_run_visible_to_session(run, "other") is False


class TestSpawnPrecisionIntegration:
    """Phase 6: Runtime isolation + origin routing + scope resolution."""

    def test_runtime_isolation_blocks_cross_runtime(self):
        cfg = resolve_runtime_isolation("agent:main:session:p1", agent_id="other")
        ok, reason = validate_runtime_isolation(cfg)
        assert ok is False
        assert "Cross-runtime" in reason

    def test_origin_routing_integration(self):
        origin = resolve_requester_origin_for_child("agent:main:session:p1")
        assert origin.channel == "agent"
        assert origin.account_id is not None

    def test_least_privilege_scopes_by_role(self):
        orch_scopes = resolve_least_privilege_scopes("main", SubagentSessionRole.ORCHESTRATOR)
        leaf_scopes = resolve_least_privilege_scopes("main", SubagentSessionRole.LEAF)
        assert len(orch_scopes) > len(leaf_scopes)
        assert "subagent:spawn" in orch_scopes
        assert "subagent:spawn" not in leaf_scopes


class TestOrphanRecoveryIntegration:
    """Phase 7: Orphan evaluation → reclassify → finalize."""

    def test_recovery_gate_wedged(self):
        import time
        old_time = time.monotonic() - 100000
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test", execution=ExecutionState(started_at=old_time),
        )
        assert evaluate_recovery_gate(run) == "wedged"

    def test_recovery_gate_aborted(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test", aborted_last_run=True,
        )
        assert evaluate_recovery_gate(run) == "aborted_last_run"

    def test_reclassify_legacy_timeout_integration(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test", aborted_last_run=True, ended_reason="timeout",
            execution=ExecutionState(status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT)),
        )
        set_run(run)
        result = reclassify_legacy_timeout(run)
        assert result is not None
        assert result.ended_reason == "interrupted"
        assert result.execution.status == ExecutionStatus.INTERRUPTED


class TestProgressHooksFullLifecycle:
    """Phase 7: Full spawned → progress → ended hook lifecycle."""

    @pytest.mark.asyncio
    async def test_full_hook_lifecycle(self):
        events = []

        async def on_spawned(run):
            events.append(("spawned", run.run_id))

        async def on_progress(run, msg):
            events.append(("progress", msg))

        async def on_ended(run):
            events.append(("ended", run.run_id))

        from future_subagent.hooks import progress as _progress
        _progress._spawned_hooks.append(on_spawned)
        _progress._progress_hooks.append(on_progress)
        _progress._ended_hooks.append(on_ended)

        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent",
            task="test",
        )
        await fire_spawned_hook(run)
        from future_subagent.hooks.progress import fire_progress_hook
        await fire_progress_hook(run, "50% done")
        await fire_ended_hook(run)

        assert events == [
            ("spawned", "r1"),
            ("progress", "50% done"),
            ("ended", "r1"),
        ]

    @pytest.mark.asyncio
    async def test_hooks_exception_isolation_in_lifecycle(self):
        events = []

        async def bad_hook(run):
            raise RuntimeError("fail")

        async def good_hook(run):
            events.append("ok")

        from future_subagent.hooks import progress as _progress
        _progress._spawned_hooks.append(bad_hook)
        _progress._spawned_hooks.append(good_hook)
        await fire_spawned_hook(SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test"
        ))
        assert "ok" in events


class TestSwarmRecordFields:
    """Verify all new SubagentRunRecord fields from v3 plan."""

    def test_swarm_fields(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test",
            swarm_group_id="g1",
            swarm_run_state=SwarmRunState.ACTIVE.value,
        )
        assert run.swarm_group_id == "g1"
        assert run.swarm_run_state == "active"

    def test_thread_binding_info_field(self):
        info = ThreadBindingInfo(thread_id="t1", delivery_origin="origin")
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test",
            thread_binding_info=info,
        )
        assert run.thread_binding_info is not None
        assert run.thread_binding_info.thread_id == "t1"

    def test_suppress_completion_delivery_field(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test",
            suppress_completion_delivery=True,
        )
        assert run.suppress_completion_delivery is True

    def test_retain_attachments_on_keep_field(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test",
            retain_attachments_on_keep=True,
        )
        assert run.retain_attachments_on_keep is True

    def test_transcript_target_field(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test",
        )
        assert run.execution.transcript_target is None
        updated = run.model_copy(update={
            "execution": run.execution.model_copy(update={"transcript_target": "target_session"}),
        })
        assert updated.execution.transcript_target == "target_session"

    def test_kill_reconciliation_field(self):
        kr = KillReconciliationState(reconciled=False, killed_at=100.0)
        run = SubagentRunRecord(
            run_id="r1", child_session_key="child", requester_session_key="parent", task="test",
            kill_reconciliation=kr,
        )
        assert run.kill_reconciliation is not None
        assert run.kill_reconciliation.killed_at == 100.0
