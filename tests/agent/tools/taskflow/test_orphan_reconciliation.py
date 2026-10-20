"""Orphan-flow reconciliation: conservative, proof-based, never kills a child.

These tests pin the three safety rules that make the scan safe to run at boot:

* a live owner or ANY live child vetoes the scan;
* a missing session is only acted on after the grace window (and the answer
  must come from the server hook, not from a guess);
* finalization only marks the flow/its steps cancelled — it never touches the
  child sessions or deletes a row.
"""

from __future__ import annotations

import pytest

from agent.tools.taskflow import reconcile
from agent.tools.taskflow.config import StepStatus, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]

SESSION = "sess-orphan-owner"


@pytest.fixture(autouse=True)
def _fresh_grace_state():
    reconcile.reset_missing_state()
    yield
    reconcile.reset_missing_state()


async def _flow(
    *,
    flow_id: str = "flow-1",
    status: str = TaskFlowStatus.RUNNING.value,
    steps: list[dict] | None = None,
    wait: dict | None = None,
) -> None:
    await store_sqlite.create_flow(
        flow_id,
        {"steps": steps or [], "results": []},
        session_id=SESSION,
        status=status,
    )
    if wait is not None:
        flow = await store_sqlite.get_flow(flow_id, SESSION)
        assert flow is not None
        await store_sqlite.update_flow(
            flow_id, flow["expected_revision"], session_id=SESSION, wait=wait
        )


def _step(step_id: str, status: str, child: str = "") -> dict:
    step = {"step_id": step_id, "task": "task", "status": status}
    if child:
        step["child_session_key"] = child
    return step


@pytest.mark.asyncio
async def test_owner_missing_finalizes_after_the_grace_window(isolated_db):
    await _flow(
        steps=[
            _step("s1", StepStatus.DONE.value, "child-done"),
            _step("s2", StepStatus.DISPATCHED.value, "child-running"),
        ]
    )

    def dead(_key: str) -> bool:
        return False

    def missing(_sid: str) -> bool:
        return False

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=missing, child_is_live=dead, now=1000.0, grace_seconds=300.0
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value

    assert await reconcile.reconcile_orphaned_flows(
        session_exists=missing, child_is_live=dead, now=1301.0, grace_seconds=300.0
    ) == ["flow-1"]
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.CANCELLED.value
    assert "orphaned" in flow["state"]["cancel_reason"]
    assert flow["wait"] is None
    by_id = {step["step_id"]: step for step in flow["state"]["steps"]}
    assert by_id["s1"]["status"] == StepStatus.DONE.value
    assert by_id["s2"]["status"] == StepStatus.CANCELLED.value
    assert "orphaned" in by_id["s2"]["cancel_reason"]


@pytest.mark.asyncio
async def test_a_live_owner_is_never_touched(isolated_db):
    await _flow(steps=[_step("s1", StepStatus.DISPATCHED.value, "child-1")])

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: True,
            child_is_live=lambda _key: False,
            grace_seconds=0.0,
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_any_live_child_vetoes_even_when_the_owner_is_missing(isolated_db):
    await _flow(
        steps=[
            _step("s1", StepStatus.DISPATCHED.value, "child-dead"),
            _step("s2", StepStatus.DISPATCHED.value, "child-live"),
        ]
    )

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: False,
            child_is_live=lambda key: key == "child-live",
            grace_seconds=0.0,
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_child_that_starts_during_grace_clears_the_candidate(isolated_db):
    await _flow(steps=[_step("s1", StepStatus.DISPATCHED.value, "child-1")])
    live = False

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: False,
            child_is_live=lambda _key: live,
            now=1000.0,
            grace_seconds=300.0,
        )
        == []
    )
    live = True
    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: False,
            child_is_live=lambda _key: live,
            now=1301.0,
            grace_seconds=300.0,
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_an_owner_that_reappears_is_not_cancelled(isolated_db):
    await _flow(steps=[_step("s1", StepStatus.READY.value)])
    alive = False

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: alive,
            child_is_live=lambda _key: False,
            now=1000.0,
            grace_seconds=300.0,
        )
        == []
    )
    alive = True
    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: alive,
            child_is_live=lambda _key: False,
            now=1301.0,
            grace_seconds=300.0,
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_an_unreadable_owner_check_fails_open(isolated_db):
    await _flow(steps=[_step("s1", StepStatus.READY.value)])

    def boom(_sid: str) -> bool:
        raise RuntimeError("messages store unavailable")

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=boom, child_is_live=lambda _key: False, grace_seconds=0.0
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_missing_session_hook_is_a_no_op(isolated_db, monkeypatch):
    await _flow(steps=[_step("s1", StepStatus.READY.value)])
    monkeypatch.setattr(reconcile.hooks, "resolve", lambda _name: None)

    assert await reconcile.reconcile_orphaned_flows(child_is_live=lambda _key: False) == []
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_the_default_hook_and_registry_path_finalizes(isolated_db):
    """The production seams (runtime hook + real registry liveness) are wired."""
    from runtime import hooks

    hooks.register(hooks.SESSION_EXISTS, lambda _sid: False)
    try:
        await _flow(steps=[_step("s1", StepStatus.DISPATCHED.value, "child-missing")])
        assert await reconcile.reconcile_orphaned_flows(grace_seconds=0.0) == ["flow-1"]
    finally:
        hooks.unregister(hooks.SESSION_EXISTS)
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_the_sweeper_wrapper_uses_the_reconciler(isolated_db, monkeypatch):
    """The lifecycle sweeper's taskflow hook is wired to the reconciler."""
    from config.features import TASKFLOW_INFRA
    from runtime import hooks
    from agent.tools.subagent.registry import sweeper

    monkeypatch.setitem(TASKFLOW_INFRA, "orphan_grace_seconds", 0.0)
    hooks.register(hooks.SESSION_EXISTS, lambda _sid: False)
    try:
        await _flow(steps=[_step("s1", StepStatus.READY.value)])
        assert await sweeper._reconcile_orphaned_taskflows() == 1
    finally:
        hooks.unregister(hooks.SESSION_EXISTS)
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_a_terminal_flow_is_not_scanned(isolated_db):
    await _flow(status=TaskFlowStatus.DONE.value, steps=[_step("s1", StepStatus.DONE.value)])

    assert (
        await reconcile.reconcile_orphaned_flows(
            session_exists=lambda _sid: False,
            child_is_live=lambda _key: False,
            grace_seconds=0.0,
        )
        == []
    )
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.DONE.value


@pytest.mark.asyncio
async def test_a_waiting_flow_with_no_child_is_finalized(isolated_db):
    await _flow(steps=[], wait={"reason": "external", "set_at": 1.0})

    assert await reconcile.reconcile_orphaned_flows(
        session_exists=lambda _sid: False,
        child_is_live=lambda _key: False,
        grace_seconds=0.0,
    ) == ["flow-1"]
    flow = await store_sqlite.get_flow("flow-1", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.CANCELLED.value
    assert flow["wait"] is None
