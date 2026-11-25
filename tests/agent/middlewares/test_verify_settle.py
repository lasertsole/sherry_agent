"""Verify-completion settle wait: let in-flight step children flush their edits.

A step child can report ``done`` while its file writes are still in flight.
Completing verify in that window yields a false finish (the user sees "done"
but the last edits are missing). The gate waits a bounded interval for in-flight
TaskFlow steps to settle before telling the model verify can complete; on
timeout it injects a warning but never wedges completion.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from langchain_core.messages import HumanMessage

import agent.middlewares.todo_continuation.core as tc
from agent.tools.todolist import stagnation_tracker as st
from agent.tools.todolist import service as todo_service
from agent.tools.todolist.registry import store_sqlite as todo_store
from config.features import TODOLIST_INFRA
from runtime import hooks

pytestmark = [pytest.mark.unit]

_SID = "sess-settle"
_SETTLE_WARNING_MARKER = "[WARNING: SUB-TASKS STILL RUNNING]"


@pytest.fixture(autouse=True)
def _clean() -> None:
    st.reset(_SID)
    yield
    st.reset(_SID)


def _flow_with_step(status: str) -> dict:
    return {
        "flow_id": "flow-1",
        "status": "running",
        "state": {
            "description": "work",
            "steps": [{"step_id": "s1", "task": "t", "depends_on": [], "status": status}],
        },
    }


# -- _wait_for_step_settle ---------------------------------------------------


@pytest.mark.asyncio
async def test_settle_returns_true_when_no_step_in_flight(monkeypatch):
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    monkeypatch.setattr(flow_store, "get_active_flows_sync", lambda sid: [])
    assert await tc._wait_for_step_settle(_SID, timeout_s=1.0) is True


@pytest.mark.asyncio
async def test_settle_waits_then_returns_true_when_step_settles(monkeypatch):
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    states = iter([_flow_with_step("dispatched"), _flow_with_step("done")])
    monkeypatch.setattr(flow_store, "get_active_flows_sync", lambda sid: next(states, []))
    monkeypatch.setattr(tc, "_SETTLE_POLL_S", 0.01)
    assert await tc._wait_for_step_settle(_SID, timeout_s=1.0) is True


@pytest.mark.asyncio
async def test_settle_returns_false_on_timeout(monkeypatch):
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    monkeypatch.setattr(
        flow_store, "get_active_flows_sync", lambda sid: [_flow_with_step("dispatched")]
    )
    monkeypatch.setattr(tc, "_SETTLE_POLL_S", 0.01)
    assert await tc._wait_for_step_settle(_SID, timeout_s=0.03) is False


@pytest.mark.asyncio
async def test_settle_fail_open_on_store_error(monkeypatch):
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    def _boom(_sid):
        raise RuntimeError("store down")

    monkeypatch.setattr(flow_store, "get_active_flows_sync", _boom)
    assert await tc._wait_for_step_settle(_SID, timeout_s=1.0) is True


def test_settle_timeout_config_exists():
    assert TODOLIST_INFRA["verify_settle_wait_s"] == 30.0


# -- _build_verify_block -----------------------------------------------------


def test_verify_block_appends_warning_on_settle_timeout():
    block = tc._build_verify_block({"status": "pending"}, "finished", settle_timed_out=True)
    assert _SETTLE_WARNING_MARKER in block
    assert "read its report" in block  # original directive preserved


def test_verify_block_no_warning_when_settled():
    block = tc._build_verify_block({"status": "pending"}, "finished")
    assert _SETTLE_WARNING_MARKER not in block


# -- middleware integration --------------------------------------------------


def _state():
    return {
        "session_id": _SID,
        "messages": [
            HumanMessage(
                content="[SYSTEM DIRECTIVE: TODO CONTINUATION] keep going",
                metadata={"origin": "todo_continuation", "internal": True},
            )
        ],
    }


@pytest.fixture()
def spy_auto_turn() -> Iterator[list[HumanMessage]]:
    calls: list[HumanMessage] = []

    async def _spy(session_key: str, injection: HumanMessage) -> object:
        calls.append(injection)
        return object()

    hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, _spy)
    yield calls
    hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)


@pytest.fixture(autouse=True)
def _verify_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify-only: work todo completed, no in-flight step in the flow rows."""
    monkeypatch.setattr(
        todo_store,
        "get_todos_sync",
        lambda sid: [
            {
                "content": "done work",
                "status": "completed",
                "priority": "high",
                "flow_id": None,
                "step_id": None,
            },
            {
                "content": "[VERIFY] acceptance",
                "status": "pending",
                "priority": "high",
                "flow_id": None,
                "step_id": None,
                "metadata": {"kind": "verify", "auto_inserted": True},
                "subagent_id": "reviewer-1",
            },
        ],
    )
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    monkeypatch.setattr(flow_store, "get_active_flows_sync", lambda sid: [])
    monkeypatch.setattr(todo_service, "is_verify_todo", lambda todo: bool(todo.get("metadata")))
    monkeypatch.setattr(todo_service, "verify_subagent_state", lambda _todo: "finished")
    monkeypatch.setattr(todo_service, "verify_respawns", lambda _todo: 0)
    monkeypatch.setattr(todo_service, "verify_budget_exhausted", lambda _sid: False)


@pytest.mark.asyncio
async def test_finished_reviewer_with_settle_timeout_warns(spy_auto_turn, monkeypatch):
    # The verify-only condition holds (flow rows empty), but the settle check
    # sees an in-flight child that never settles within the window.
    monkeypatch.setattr(tc, "_has_in_flight_steps", lambda flows: True)

    async def _timeout(_sid, _timeout):
        return False

    monkeypatch.setattr(tc, "_wait_for_step_settle", _timeout)

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert len(spy_auto_turn) == 1
    prompt = spy_auto_turn[0].content
    assert "[VERIFY TODO]" in prompt
    assert _SETTLE_WARNING_MARKER in prompt


@pytest.mark.asyncio
async def test_no_in_flight_child_skips_wait_and_warning(spy_auto_turn, monkeypatch):
    waited = {"n": 0}

    async def _wait(_sid, _timeout):
        waited["n"] += 1
        return True

    monkeypatch.setattr(tc, "_wait_for_step_settle", _wait)

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert waited["n"] == 0  # no in-flight child -> no wait
    assert len(spy_auto_turn) == 1
    assert _SETTLE_WARNING_MARKER not in spy_auto_turn[0].content
