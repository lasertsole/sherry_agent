import pytest
from future_subagent.control.kill import (
    resolve_kill_target_state,
    list_killable_children,
)
from future_subagent.registry.memory import set_run, clear
from future_subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    ExecutionStatus,
    KillReconciliationState,
    RunOutcome,
    RunOutcomeStatus,
)


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:future_subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestResolveKillTargetState:
    def test_terminal(self):
        run = _make_run(
            execution=ExecutionState(status=ExecutionStatus.TERMINAL),
        )
        assert resolve_kill_target_state(run) == "terminal"

    def test_finalizing(self):
        kr = KillReconciliationState(reconciled=False)
        run = _make_run(
            execution=ExecutionState(status=ExecutionStatus.RUNNING),
            kill_reconciliation=kr,
        )
        assert resolve_kill_target_state(run) == "finalizing"

    def test_killable(self):
        run = _make_run(
            execution=ExecutionState(status=ExecutionStatus.RUNNING),
        )
        assert resolve_kill_target_state(run) == "killable"

    def test_killable_interrupted(self):
        run = _make_run(
            execution=ExecutionState(status=ExecutionStatus.INTERRUPTED),
        )
        assert resolve_kill_target_state(run) == "killable"

    def test_reconciled_kill_not_finalizing(self):
        kr = KillReconciliationState(reconciled=True)
        run = _make_run(
            execution=ExecutionState(status=ExecutionStatus.RUNNING),
            kill_reconciliation=kr,
        )
        assert resolve_kill_target_state(run) == "killable"


class TestListKillableChildren:
    def test_running_children(self):
        r1 = _make_run(run_id="r1", requester_session_key="agent:main:session:p1",
                        child_session_key="agent:main:future_subagent:c1")
        set_run(r1)
        result = list_killable_children("agent:main:session:p1")
        assert len(result) == 1

    def test_terminal_children_not_listed(self):
        r1 = _make_run(run_id="r1", requester_session_key="agent:main:session:p1",
                        child_session_key="agent:main:future_subagent:c1",
                        execution=ExecutionState(status=ExecutionStatus.TERMINAL,
                                                 outcome=RunOutcome(status=RunOutcomeStatus.OK)))
        set_run(r1)
        result = list_killable_children("agent:main:session:p1")
        assert len(result) == 0

    def test_no_children(self):
        result = list_killable_children("nonexistent")
        assert result == []
