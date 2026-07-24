import pytest
import asyncio
from future_subagent.registry.settle_wake import (
    SettleWakeState,
    RequesterSettleWakeBatch,
    get_settle_wake_batch,
)
from future_subagent.registry.memory import set_run, clear
from future_subagent.types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome, RunOutcomeStatus


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


class TestSettleWakeState:
    def test_values(self):
        assert SettleWakeState.IDLE == "idle"
        assert SettleWakeState.COMPLETING == "completing"
        assert SettleWakeState.SETTLED == "settled"
        assert SettleWakeState.DONE == "done"


class TestRequesterSettleWakeBatch:
    def test_register_run(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        assert "parent1" in batch._pending
        assert "r1" in batch._pending["parent1"]
        assert batch._state["parent1"] == SettleWakeState.IDLE

    def test_register_multiple_runs(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.register_run_for_settle("r2", "parent1")
        assert len(batch._pending["parent1"]) == 2

    def test_transition_idle_to_completing(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        result = batch.transition_batch("parent1", "child_completed")
        assert result == SettleWakeState.COMPLETING

    def test_transition_completing_to_settled(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.transition_batch("parent1", "child_completed")
        result = batch.transition_batch("parent1", "all_settled")
        assert result == SettleWakeState.SETTLED

    def test_transition_settled_to_done(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.transition_batch("parent1", "child_completed")
        batch.transition_batch("parent1", "all_settled")
        result = batch.transition_batch("parent1", "woke")
        assert result == SettleWakeState.DONE

    def test_transition_done_to_completing_on_new_child(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.transition_batch("parent1", "child_completed")
        batch.transition_batch("parent1", "all_settled")
        batch.transition_batch("parent1", "woke")
        result = batch.transition_batch("parent1", "new_child")
        assert result == SettleWakeState.COMPLETING

    def test_transition_invalid_stays(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        result = batch.transition_batch("parent1", "invalid_event")
        assert result == SettleWakeState.IDLE

    def test_transition_unregistered(self):
        batch = RequesterSettleWakeBatch()
        result = batch.transition_batch("nonexistent", "child_completed")
        assert result == SettleWakeState.COMPLETING

    def test_retire_after_settle(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.transition_batch("parent1", "child_completed")
        batch.retire_after_settle("parent1")
        assert "parent1" not in batch._state
        assert "parent1" not in batch._pending

    @pytest.mark.asyncio
    async def test_retire_cancels_timer(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.schedule_settle_wake_retry("parent1", delay=100.0)
        batch.retire_after_settle("parent1")
        assert "parent1" not in batch._timers

    @pytest.mark.asyncio
    async def test_schedule_settle_wake_retry(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.schedule_settle_wake_retry("parent1", delay=100.0)
        assert "parent1" in batch._timers
        assert batch._rearms["parent1"] == 1

    @pytest.mark.asyncio
    async def test_schedule_skips_if_existing_timer(self):
        batch = RequesterSettleWakeBatch()
        batch.register_run_for_settle("r1", "parent1")
        batch.schedule_settle_wake_retry("parent1", delay=100.0)
        batch.schedule_settle_wake_retry("parent1", delay=100.0)
        assert batch._rearms["parent1"] == 1

    def test_full_lifecycle(self):
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


class TestGetSettleWakeBatch:
    def test_singleton(self):
        b1 = get_settle_wake_batch()
        b2 = get_settle_wake_batch()
        assert b1 is b2
