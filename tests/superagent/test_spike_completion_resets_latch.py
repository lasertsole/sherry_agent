"""P-Spike 1: a subagent completion resets the main session's continuation latch.

The super-agent L7 model relies on workers being children of the persistent
main session: when a worker finishes, its completion carrier is a non-continuation
turn, so ``PlanContinuationEnforcer`` clears the todo continuation budget and
stop latch. This spike verifies that contract without spawning a real worker or
running an LLM turn.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

import agent.middlewares.todo_continuation.core as tc
from agent.tools.todolist import stagnation_tracker as st
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.taskflow.registry import store_sqlite as flow_store
from runtime import hooks

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_SID = "superagent-spike-latch"


@pytest.fixture(autouse=True)
def _clean_tracker() -> None:
    st.reset(_SID)
    yield
    st.reset(_SID)


@pytest.mark.asyncio
async def test_subagent_completion_turn_resets_budget_and_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker completion is a NEW event, not another gate-injected turn."""
    monkeypatch.setattr(
        todo_store,
        "get_todos_sync",
        lambda session_id: [
            {
                "content": "worker finished the ticket",
                "status": "pending",
                "priority": "high",
                "flow_id": None,
                "step_id": None,
            }
        ],
    )
    monkeypatch.setattr(flow_store, "get_active_flows_sync", lambda session_id: [])
    # No auto-turn hook: the reset branch runs before delivery, so the spike
    # stays hermetic and the counter remains 0 after the reset.
    hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)

    st._continuation_turns[_SID] = 4
    st.mark_stopped(_SID)
    assert st.continuation_turns(_SID) == 4
    assert st.is_stopped(_SID) is True

    state = {
        "session_id": _SID,
        "messages": [
            HumanMessage(
                content="[subagent:run-1] child finished",
                metadata={"internal": True, "provenance": "subagent_completion"},
            )
        ],
    }
    await tc.PlanContinuationEnforcer().aafter_agent(state)

    assert st.continuation_turns(_SID) == 0, "continuation budget must restart"
    assert st.is_stopped(_SID) is False, "stop latch must be released"
