import pytest
from agent.tools.subagent.registry.run_manager import (
    register_run,
    mark_run_paused_after_yield,
    mark_run_running,
    complete_run,
)
from agent.tools.subagent.registry.memory import get, clear
from agent.tools.subagent.types.registry import ExecutionStatus, RunOutcome, RunOutcomeStatus


pytestmark = [pytest.mark.unit]


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
        assert run.execution.status == ExecutionStatus.PENDING
        assert run.execution.started_at is None
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

    def test_max_output_tokens_is_frozen_on_the_run(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            max_output_tokens=16384,
        )
        assert run.max_output_tokens == 16384

    def test_depth_0_is_main(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            depth=0,
        )
        from agent.tools.subagent.types.capability import SubagentSessionRole

        assert run.role == SubagentSessionRole.MAIN

    def test_depth_at_max_is_leaf(self):
        from agent.tools.subagent.config import get_config

        config = get_config()
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            depth=config.max_spawn_depth,
        )
        from agent.tools.subagent.types.capability import SubagentSessionRole

        assert run.role == SubagentSessionRole.LEAF

    def test_registration_always_requires_completion(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        from agent.tools.subagent.types.registry import DeliveryStatus

        assert run.completion.required is True
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

    def test_pending_to_running_stamps_started_at(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        assert run.execution.status == ExecutionStatus.PENDING
        assert run.execution.started_at is None

        updated = mark_run_running(run.run_id)
        assert updated.execution.status == ExecutionStatus.RUNNING
        assert updated.execution.started_at is not None

    def test_running_is_noop(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        first = mark_run_running(run.run_id)
        started_at = first.execution.started_at

        second = mark_run_running(run.run_id)
        assert second.execution.status == ExecutionStatus.RUNNING
        assert second.execution.started_at == started_at

    def test_terminal_is_noop(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        complete_run(run.run_id, RunOutcome(status=RunOutcomeStatus.KILLED))
        updated = mark_run_running(run.run_id)
        assert updated.execution.status == ExecutionStatus.TERMINAL

    def test_missing(self):
        assert mark_run_running("nonexistent") is None


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

    def test_complete_uses_the_legacy_cap_when_the_model_is_unset(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
        )
        updated = complete_run(
            run.run_id,
            RunOutcome(status=RunOutcomeStatus.OK),
            result_text="x" * 30_000,
        )
        assert len(updated.completion.result_text or "") < 30_000
        assert "truncated" in (updated.completion.result_text or "")

    def test_complete_uses_the_scaled_cap_for_a_configured_model(self):
        run = register_run(
            child_session_key="agent:main:subagent:abc",
            requester_session_key="agent:main:session:p1",
            task="test",
            max_output_tokens=16384,
        )
        updated = complete_run(
            run.run_id,
            RunOutcome(status=RunOutcomeStatus.OK),
            result_text="x" * 60_000,
        )
        assert len(updated.completion.result_text or "") > 49_000
        assert "truncated" in (updated.completion.result_text or "")

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


class TestHandoffPersistence:
    def test_register_run_keeps_the_taskflow_coordinates(self):
        run = register_run(
            child_session_key="agent:main:subagent:coord",
            requester_session_key="agent:main:session:p1",
            task="t",
            step_id="step-1",
            flow_id="flow-1",
        )

        assert run.step_id == "step-1"
        assert run.flow_id == "flow-1"

    def test_complete_run_stores_the_handoff(self):
        run = register_run(
            child_session_key="agent:main:subagent:ho",
            requester_session_key="agent:main:session:p1",
            task="t",
        )
        handoff = {
            "task_understanding": "u",
            "discovered_hard_constraints": [],
            "locked_decisions": [],
            "failed_branches": [],
        }

        updated = complete_run(
            run.run_id,
            RunOutcome(status=RunOutcomeStatus.OK),
            "the result",
            handoff=handoff,
        )

        assert updated is not None
        assert updated.completion.handoff == handoff
        assert updated.completion.result_text == "the result"

    def test_a_steer_resets_the_previous_handoff(self):
        from agent.tools.subagent.registry.run_manager import replace_run_after_steer

        run = register_run(
            child_session_key="agent:main:subagent:steer",
            requester_session_key="agent:main:session:p1",
            task="t",
        )
        complete_run(
            run.run_id,
            RunOutcome(status=RunOutcomeStatus.OK),
            "old",
            handoff={"task_understanding": "old"},
        )

        updated = replace_run_after_steer(run.run_id, "new task")

        assert updated is not None
        assert updated.completion.handoff is None
        assert updated.completion.result_text is None
