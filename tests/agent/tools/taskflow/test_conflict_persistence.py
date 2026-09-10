"""Regression tests: spawned children survive optimistic-lock conflicts.

BLOCKER under test (todo 2/3, taskflow-dag-phase1): ``taskflow_run_task`` and
``taskflow_dispatch`` spawn the detached child BEFORE ``update_flow``. If that
write loses the optimistic-lock race (``FlowConflictError``) the child is left
unrecorded and a retry re-spawns it. These tests inject a concurrent writer
between the read and the persist and prove:

1. ``taskflow_run_task`` records the already-spawned child after a bounded
   retry, recomputing the step id from the FRESH step count.
2. ``taskflow_dispatch`` records every spawned child on both the all-success
   and the partial-failure persist paths.
3. When retries are exhausted the returned ``Error:`` NAMES the spawned child
   key(s) instead of silently dropping them.
4. ``taskflow_wait_all`` converts a registry exception into an ``Error:`` string
   and re-attempts a transient registry import instead of caching it forever.

The spawn pipeline is never invoked: ``_dispatch.dispatch_child`` is always a
fake. The injected conflict is purely a monkeypatched ``store_sqlite.update_flow``.
"""

import builtins
import importlib
from pathlib import Path

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.registry.store_sqlite import FlowConflictError
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch
from agent.tools.taskflow.tools.taskflow_run_task import taskflow_run_task

# The family package re-exports the wait_all tool object under the same name,
# shadowing the submodule for package-attribute traversal (see TODO 8 note).
wait_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_wait_all")

pytestmark = [pytest.mark.unit]

_SESSION = "sess-1"
_CHILD_1 = "agent:main:subagent:child-1"
_CHILD_2 = "agent:main:subagent:child-2"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fake_dispatch(*keys: str, fail_at: int | None = None):
    """Fake seam returning ``keys`` in order; raises RuntimeError on the Nth call."""
    calls: list = []

    async def _call(task: str, requester_session_key: str, label: str | None = None) -> str:
        calls.append((task, requester_session_key, label))
        index = len(calls)
        if fail_at is not None and index == fail_at:
            raise RuntimeError(f"spawn rejected for call {index}")
        return keys[index - 1]

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


async def _bump_revision(flow_id: str, real_update) -> None:
    """Concurrent writer: bump the revision without touching the step list."""
    current = await store_sqlite.get_flow(flow_id)
    assert current is not None
    await real_update(flow_id, current["expected_revision"], wait={"reason": "concurrent"})


async def _append_concurrent_step(flow_id: str, real_update) -> None:
    """Concurrent writer: append a step-1 so the retry MUST recompute its id."""
    current = await store_sqlite.get_flow(flow_id)
    assert current is not None
    state = dict(current["state"])
    steps = list(state.get("steps") or [])
    steps.append({"step_id": "step-1", "task": "concurrent", "depends_on": [], "status": "ready"})
    state["steps"] = steps
    await real_update(flow_id, current["expected_revision"], state=state)


def _patch_conflict_once(monkeypatch: pytest.MonkeyPatch, concurrent_write) -> dict:
    """Make the FIRST update_flow lose the race, then delegate to the real one.

    ``concurrent_write(flow_id, real_update)`` performs a genuine concurrent
    write with the real ``update_flow`` (captured before patching), after which
    the wrapper raises ``FlowConflictError`` carrying the REAL latest revision.
    Later calls delegate to the real ``update_flow``, so the retry path is what
    actually persists.
    """
    real_update = store_sqlite.update_flow
    fired = {"done": False}

    async def wrapper(flow_id: str, expected_revision: int, **kwargs):
        if not fired["done"]:
            fired["done"] = True
            await concurrent_write(flow_id, real_update)
            fresh = await store_sqlite.get_flow(flow_id)
            assert fresh is not None
            raise FlowConflictError(flow_id, expected_revision, fresh["expected_revision"])
        return await real_update(flow_id, expected_revision, **kwargs)

    monkeypatch.setattr(store_sqlite, "update_flow", wrapper)
    return fired


def _always_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wrapper(flow_id: str, expected_revision: int, **_kwargs):
        raise FlowConflictError(flow_id, expected_revision, expected_revision + 1)

    monkeypatch.setattr(store_sqlite, "update_flow", wrapper)


async def _seed(flow_id: str, steps: list[dict]) -> None:
    out = await taskflow_create.coroutine(
        flow_id=flow_id, description="conflict probe", initial_state={"steps": steps}
    )
    assert "Error" not in out, out


def _step(step_id: str, status: str) -> dict:
    return {"step_id": step_id, "task": f"task-{step_id}", "depends_on": [], "status": status}


async def _flow(flow_id: str) -> dict:
    flow = await store_sqlite.get_flow(flow_id)
    assert flow is not None
    return flow


# ---------------------------------------------------------------------------
# taskflow_run_task: conflict between spawn and persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_records_spawned_child_after_conflict(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow and a spawned child, with a concurrent writer bumping the
    # revision exactly once between the pre-spawn read and the persist
    monkeypatch.setattr(_dispatch, "dispatch_child", _fake_dispatch(_CHILD_1))
    _patch_conflict_once(monkeypatch, _bump_revision)
    await taskflow_create.coroutine(flow_id="flow-1", description="run conflict")

    # When the dispatched step is persisted
    out = await taskflow_run_task.coroutine(flow_id="flow-1", task="collect", session_id=_SESSION)

    # Then the already-spawned child IS recorded (never lost to the conflict)
    assert out.startswith("TaskFlow step dispatched"), out
    assert _CHILD_1 in out
    flow = await _flow("flow-1")
    steps = flow["state"]["steps"]
    assert steps[-1]["child_session_key"] == _CHILD_1
    assert steps[-1]["status"] == "dispatched"
    # create (rev 1) -> concurrent bump (rev 2) -> persisted dispatch (rev 3)
    assert flow["expected_revision"] == 3


@pytest.mark.asyncio
async def test_run_task_recomputes_step_id_from_fresh_steps_after_conflict(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a concurrent writer that appends step-1 during the spawn window
    monkeypatch.setattr(_dispatch, "dispatch_child", _fake_dispatch(_CHILD_1))
    _patch_conflict_once(monkeypatch, _append_concurrent_step)
    await taskflow_create.coroutine(flow_id="flow-1", description="id collision")

    # When the dispatched step is persisted after the retry
    out = await taskflow_run_task.coroutine(flow_id="flow-1", task="collect", session_id=_SESSION)

    # Then the id was recomputed from the FRESH step count: step-2, no collision
    assert out.startswith("TaskFlow step dispatched"), out
    assert "step_id=step-2" in out
    flow = await _flow("flow-1")
    steps = flow["state"]["steps"]
    assert [s["step_id"] for s in steps] == ["step-1", "step-2"]
    assert steps[1]["task"] == "collect"
    assert steps[1]["child_session_key"] == _CHILD_1


@pytest.mark.asyncio
async def test_run_task_retries_exhausted_names_spawned_child(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given every persist attempt conflicts
    monkeypatch.setattr(_dispatch, "dispatch_child", _fake_dispatch(_CHILD_1))
    _always_conflict(monkeypatch)
    await taskflow_create.coroutine(flow_id="flow-1", description="exhaust")

    # When the retry budget is exhausted
    out = await taskflow_run_task.coroutine(flow_id="flow-1", task="collect", session_id=_SESSION)

    # Then the error NAMES the already-spawned child so the caller can recover
    assert out.startswith("Error:"), out
    assert _CHILD_1 in out


# ---------------------------------------------------------------------------
# taskflow_dispatch: conflict on the all-success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_all_success_records_children_after_conflict(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given two ready steps, both spawned, and a one-shot conflict
    fake = _fake_dispatch(_CHILD_1, _CHILD_2)
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_conflict_once(monkeypatch, _bump_revision)
    await _seed("flow-1", [_step("step-1", "ready"), _step("step-2", "ready")])

    # When the batch is persisted
    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    # Then BOTH spawned children are recorded after the retry
    assert out.startswith("TaskFlow steps dispatched"), out
    assert len(fake.calls) == 2  # no hidden re-spawn
    flow = await _flow("flow-1")
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert by_id["step-1"]["child_session_key"] == _CHILD_1
    assert by_id["step-2"]["child_session_key"] == _CHILD_2
    assert by_id["step-1"]["status"] == "dispatched"
    assert by_id["step-2"]["status"] == "dispatched"


# ---------------------------------------------------------------------------
# taskflow_dispatch: conflict on the partial-failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_partial_failure_records_success_after_conflict(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a mid-batch spawn failure AND a one-shot conflict on the persist
    fake = _fake_dispatch(_CHILD_1, fail_at=2)
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _patch_conflict_once(monkeypatch, _bump_revision)
    await _seed("flow-1", [_step("step-1", "ready"), _step("step-2", "ready")])

    # When dispatch runs
    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    # Then the failure is reported AND the spawned child is recorded after retry
    assert out.startswith("Error:"), out
    assert "step-1" in out and "step-2" in out
    assert len(fake.calls) == 2
    flow = await _flow("flow-1")
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert by_id["step-1"]["status"] == "dispatched"
    assert by_id["step-1"]["child_session_key"] == _CHILD_1
    assert by_id["step-2"]["status"] == "ready"
    assert "child_session_key" not in by_id["step-2"]


@pytest.mark.asyncio
async def test_dispatch_retries_exhausted_names_spawned_children(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given every persist attempt conflicts
    fake = _fake_dispatch(_CHILD_1, _CHILD_2)
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    _always_conflict(monkeypatch)
    await _seed("flow-1", [_step("step-1", "ready"), _step("step-2", "ready")])

    # When the retry budget is exhausted
    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    # Then the error NAMES every already-spawned child so none is silently lost
    assert out.startswith("Error:"), out
    assert _CHILD_1 in out
    assert _CHILD_2 in out


# ---------------------------------------------------------------------------
# taskflow_wait_all: registry failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_all_registry_exception_returns_error(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a registry lookup that raises at call time (the real seam is sync)
    def _boom(_key: str):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(wait_mod, "get_run_by_child_session_key", _boom)
    monkeypatch.setattr(wait_mod, "is_live_unended_run", lambda _run: True)
    await store_sqlite.create_flow(
        "flow-wait",
        {"description": "wait probe", "steps": [_step("step-1", "dispatched")], "results": []},
    )

    # When wait_all polls
    out = await wait_mod.taskflow_wait_all.coroutine(
        "flow-wait", timeout_seconds=0.05, poll_interval_seconds=0.01
    )

    # Then the exception becomes an Error string, never a raise
    assert out.startswith("Error:"), out
    assert "registry exploded" in out


def test_wait_all_transient_registry_import_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
):
    # Given the seam is unbound and the first registry import fails
    monkeypatch.setattr(wait_mod, "get_run_by_child_session_key", None)
    monkeypatch.setattr(wait_mod, "is_live_unended_run", None)
    monkeypatch.setattr(wait_mod, "_REGISTRY_ERROR", None)

    real_import = builtins.__import__
    attempts = {"n": 0}

    def _flaky_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "agent.tools.subagent.registry.helpers":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ImportError("transient registry failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _flaky_import)

    # When the first attempt fails
    assert wait_mod._ensure_registry() is False
    assert wait_mod._REGISTRY_ERROR is not None

    # Then a later call RE-ATTEMPTS the import instead of caching the failure
    assert wait_mod._ensure_registry() is True
    assert attempts["n"] == 2
    assert wait_mod._REGISTRY_ERROR is None
