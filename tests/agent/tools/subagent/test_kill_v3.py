import pytest
from agent.tools.subagent.control.kill import (
    resolve_kill_target_state,
    list_killable_children,
)
from agent.tools.subagent.registry.memory import set_run, clear
from agent.tools.subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    ExecutionStatus,
    KillReconciliationState,
    RunOutcome,
    RunOutcomeStatus,
)


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:subagent:abc",
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
        r1 = _make_run(
            run_id="r1",
            requester_session_key="agent:main:session:p1",
            child_session_key="agent:main:subagent:c1",
        )
        set_run(r1)
        result = list_killable_children("agent:main:session:p1")
        assert len(result) == 1

    def test_terminal_children_not_listed(self):
        r1 = _make_run(
            run_id="r1",
            requester_session_key="agent:main:session:p1",
            child_session_key="agent:main:subagent:c1",
            execution=ExecutionState(
                status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=RunOutcomeStatus.OK)
            ),
        )
        set_run(r1)
        result = list_killable_children("agent:main:session:p1")
        assert len(result) == 0

    def test_no_children(self):
        result = list_killable_children("nonexistent")
        assert result == []


@pytest.mark.asyncio
async def test_tasks_are_registered_under_the_run_id_not_the_session_key():
    """Why kill cancels by run id: the registry never keys tasks by session.

    A session-keyed queue clear (``get_task(child_session_key)``) could not match
    anything, which is why that no-op helper was removed; cancel_task(run_id) is
    the whole cancellation story.
    """
    import asyncio
    import contextlib

    from agent.tools.subagent.registry import register_task
    from agent.tools.subagent.registry.task_refs import get_task

    async def _never() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_never())
    try:
        register_task("r1", task)

        assert get_task("r1") is task
        assert get_task("agent:main:subagent:abc") is None
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
