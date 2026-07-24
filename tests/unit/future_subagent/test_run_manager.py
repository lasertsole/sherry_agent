import pytest
from future_subagent.registry.run_manager import register_run, mark_run_paused_after_yield, mark_run_running, complete_run
from future_subagent.registry.memory import get, clear
from future_subagent.types.registry import ExecutionStatus, RunOutcome, RunOutcomeStatus
from future_subagent.types.spawn import SpawnMode


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


class TestRegisterRun:
    def test_basic_registration(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="Do something",
        )
        assert run.run_id
        assert run.execution.status == ExecutionStatus.RUNNING
        assert run.execution.started_at is not None
        assert get(run.run_id) is not None

    def test_with_optional_params(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="Complex task",
            task_name="build",
            label="builder",
            depth=2,
            thinking="high",
        )
        assert run.task_name == "build"
        assert run.label == "builder"
        assert run.depth == 2
        assert run.thinking == "high"

    def test_depth_0_is_main(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            depth=0,
        )
        from future_subagent.types.capability import SubagentSessionRole
        assert run.role == SubagentSessionRole.MAIN

    def test_depth_at_max_is_leaf(self):
        from future_subagent.config import get_config
        config = get_config()
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            depth=config.max_spawn_depth,
        )
        from future_subagent.types.capability import SubagentSessionRole
        assert run.role == SubagentSessionRole.LEAF

    def test_session_mode_delivery_not_required(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            spawn_mode=SpawnMode.SESSION,
        )
        from future_subagent.types.registry import DeliveryStatus
        assert run.delivery.status == DeliveryStatus.NOT_REQUIRED

    def test_run_mode_delivery_pending(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            spawn_mode=SpawnMode.RUN,
        )
        from future_subagent.types.registry import DeliveryStatus
        assert run.delivery.status == DeliveryStatus.PENDING


class TestMarkRunPausedAfterYield:
    def test_pause(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        updated = mark_run_paused_after_yield(run.run_id)
        assert updated.execution.status == ExecutionStatus.INTERRUPTED

    def test_pause_missing(self):
        assert mark_run_paused_after_yield("nonexistent") is None


class TestMarkRunRunning:
    def test_resume(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        mark_run_paused_after_yield(run.run_id)
        updated = mark_run_running(run.run_id)
        assert updated.execution.status == ExecutionStatus.RUNNING


class TestCompleteRun:
    def test_complete_ok(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        outcome = RunOutcome(status=RunOutcomeStatus.OK)
        updated = complete_run(run.run_id, outcome, result_text="Done!")
        assert updated.execution.status == ExecutionStatus.TERMINAL
        assert updated.execution.outcome.status == RunOutcomeStatus.OK
        assert updated.completion.result_text == "Done!"

    def test_complete_error(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error="crash")
        updated = complete_run(run.run_id, outcome)
        assert updated.execution.outcome.status == RunOutcomeStatus.ERROR
        assert updated.execution.outcome.error == "crash"

    def test_complete_timeout(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        outcome = RunOutcome(status=RunOutcomeStatus.TIMEOUT, error="30s exceeded")
        updated = complete_run(run.run_id, outcome)
        assert updated.execution.outcome.status == RunOutcomeStatus.TIMEOUT

    def test_complete_missing(self):
        outcome = RunOutcome(status=RunOutcomeStatus.OK)
        assert complete_run("nonexistent", outcome) is None
