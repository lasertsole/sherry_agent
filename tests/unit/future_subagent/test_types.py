import pytest
import time
from future_subagent.types.spawn import SpawnMode, ContextMode
from future_subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    CompletionState,
    CompletionDeliveryState,
    RunOutcome,
    ExecutionStatus,
    DeliveryStatus,
    RunOutcomeStatus,
)
from future_subagent.types.lifecycle import LifecycleEndedReason, LifecycleEndedOutcome
from future_subagent.types.delivery import DeliveryContext
from future_subagent.types.capability import SubagentSessionRole, ControlScope


class TestSpawnEnums:
    def test_spawn_mode_values(self):
        assert SpawnMode.RUN == "run"
        assert SpawnMode.SESSION == "session"

    def test_context_mode_values(self):
        assert ContextMode.ISOLATED == "isolated"
        assert ContextMode.FORK == "fork"


class TestCapabilityEnums:
    def test_session_role_values(self):
        assert SubagentSessionRole.MAIN == "main"
        assert SubagentSessionRole.ORCHESTRATOR == "orchestrator"
        assert SubagentSessionRole.LEAF == "leaf"

    def test_control_scope_values(self):
        assert ControlScope.CHILDREN == "children"
        assert ControlScope.NONE == "none"


class TestLifecycleEnums:
    def test_ended_reason_values(self):
        assert LifecycleEndedReason.COMPLETE == "complete"
        assert LifecycleEndedReason.KILLED == "killed"
        assert LifecycleEndedReason.TIMEOUT == "timeout"
        assert LifecycleEndedReason.ORPHANED == "orphaned"

    def test_ended_outcome_values(self):
        assert LifecycleEndedOutcome.OK == "ok"
        assert LifecycleEndedOutcome.ERROR == "error"


class TestRegistryEnums:
    def test_execution_status(self):
        assert ExecutionStatus.RUNNING == "running"
        assert ExecutionStatus.INTERRUPTED == "interrupted"
        assert ExecutionStatus.TERMINAL == "terminal"

    def test_delivery_status(self):
        assert DeliveryStatus.NOT_REQUIRED == "not_required"
        assert DeliveryStatus.PENDING == "pending"
        assert DeliveryStatus.IN_PROGRESS == "in_progress"
        assert DeliveryStatus.DELIVERED == "delivered"
        assert DeliveryStatus.FAILED == "failed"
        assert DeliveryStatus.SUSPENDED == "suspended"
        assert DeliveryStatus.DISCARDED == "discarded"

    def test_run_outcome_status(self):
        assert RunOutcomeStatus.OK == "ok"
        assert RunOutcomeStatus.ERROR == "error"
        assert RunOutcomeStatus.TIMEOUT == "timeout"
        assert RunOutcomeStatus.UNKNOWN == "unknown"


class TestRunOutcome:
    def test_default(self):
        o = RunOutcome()
        assert o.status == RunOutcomeStatus.UNKNOWN
        assert o.error is None

    def test_with_error(self):
        o = RunOutcome(status=RunOutcomeStatus.ERROR, error="boom")
        assert o.status == RunOutcomeStatus.ERROR
        assert o.error == "boom"


class TestExecutionState:
    def test_default(self):
        s = ExecutionState()
        assert s.status == ExecutionStatus.RUNNING
        assert s.started_at is None
        assert s.outcome is None

    def test_with_values(self):
        s = ExecutionState(
            status=ExecutionStatus.TERMINAL,
            started_at=1.0,
            ended_at=2.0,
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
        )
        assert s.status == ExecutionStatus.TERMINAL
        assert s.outcome.status == RunOutcomeStatus.OK


class TestSubagentRunRecord:
    def test_minimal(self):
        r = SubagentRunRecord(
            run_id="test-1",
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:parent",
            task="Do something",
        )
        assert r.run_id == "test-1"
        assert r.spawn_mode == SpawnMode.RUN
        assert r.depth == 1
        assert r.role == SubagentSessionRole.LEAF
        assert r.execution.status == ExecutionStatus.RUNNING
        assert r.delivery.status == DeliveryStatus.NOT_REQUIRED
        assert r.cleanup == "delete"

    def test_full_construction(self):
        r = SubagentRunRecord(
            run_id="test-2",
            child_session_key="agent:main:subagent:def",
            requester_session_key="agent:main:session:parent",
            task="Complex task",
            task_name="my_task",
            spawn_mode=SpawnMode.SESSION,
            cleanup="keep",
            depth=2,
            role=SubagentSessionRole.ORCHESTRATOR,
            label="test-label",
            inherited_tool_deny=["delegate_task"],
        )
        assert r.task_name == "my_task"
        assert r.spawn_mode == SpawnMode.SESSION
        assert r.cleanup == "keep"
        assert r.depth == 2
        assert r.role == SubagentSessionRole.ORCHESTRATOR
        assert "delegate_task" in r.inherited_tool_deny

    def test_model_copy(self):
        r = SubagentRunRecord(
            run_id="test-3",
            child_session_key="agent:main:subagent:ghi",
            requester_session_key="agent:main:session:parent",
            task="Copy test",
        )
        r2 = r.model_copy(update={"depth": 5})
        assert r2.depth == 5
        assert r.depth == 1
        assert r2.run_id == "test-3"


class TestDeliveryContext:
    def test_construction(self):
        ctx = DeliveryContext(
            requester_session_key="parent",
            child_session_key="child",
            task="test",
            run_id="r1",
        )
        assert ctx.requester_session_key == "parent"
        assert ctx.depth == 1
