"""sessions_yield must treat PENDING (lane-queued) children as active."""

import pytest

from agent.tools.subagent.registry import (
    clear as clear_registry,
    complete_run,
    register_run,
    register_yield_event,
    remove_yield_event,
    wake_yield_if_all_children_settled,
)
from agent.tools.subagent.tools.sessions_yield import SessionsYieldTool
from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

pytestmark = [pytest.mark.integration]


SESSION_ID = "pending_yield"
SESSION_KEY = f"agent:main:session:{SESSION_ID}"


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    yield
    remove_yield_event(SESSION_KEY)
    clear_registry()


def _register_pending_child() -> str:
    run = register_run(
        child_session_key="agent:main:subagent:pending_yield_child",
        requester_session_key=SESSION_KEY,
        task="queued task",
        depth=1,
    )
    return run.run_id


class TestSessionsYieldPending:
    @pytest.mark.asyncio
    async def test_yield_waits_for_pending_child(self):
        _register_pending_child()
        tool = SessionsYieldTool(session_id=SESSION_ID)

        result = await tool._arun(timeout_seconds=0.15)

        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_wake_blocks_until_pending_child_settles(self):
        run_id = _register_pending_child()
        event = register_yield_event(SESSION_KEY)

        assert await wake_yield_if_all_children_settled(SESSION_KEY) is False
        assert not event.is_set()

        complete_run(run_id, RunOutcome(status=RunOutcomeStatus.OK))

        assert await wake_yield_if_all_children_settled(SESSION_KEY) is True
        assert event.is_set()
