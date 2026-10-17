"""One resume unlocking several steps dispatches them as a bounded concurrent wave.

``_auto_dispatch_ready`` spawns the newly-ready steps through
``asyncio.gather`` bounded by ``TASKFLOW_INFRA["max_parallel_dispatch"]``, and
every spawned key is persisted in one CAS-retried write. These tests pin the
three things that could silently regress: every key lands on its own step, the
in-flight spawn count never exceeds the cap, and a cap of 1 restores the
historical sequential behaviour exactly.
"""

import asyncio
from pathlib import Path

import pytest

from config.features import TASKFLOW_INFRA

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-wave"
_ROOT_CHILD = "agent:main:subagent:root"


def _step(step_id: str, status: str, *, child: str = "", depends_on: list[str] | None = None):
    step: dict = {
        "step_id": step_id,
        "task": f"task-{step_id}",
        "depends_on": depends_on or [],
        "status": status,
    }
    if child:
        step["child_session_key"] = child
    return step


async def _seed_flow_with_fanout(flow_id: str, dependents: int) -> None:
    steps = [_step("step-root", "dispatched", child=_ROOT_CHILD)]
    for index in range(1, dependents + 1):
        steps.append(_step(f"step-{index}", "blocked", depends_on=["step-root"]))
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="fan-out probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


def _tracking_dispatch(peak: dict[str, int], *, delay: float = 0.0, fail_marker: str = ""):
    """Fake spawn seam recording the peak in-flight count (and per-task failures)."""
    state = {"active": 0, "calls": 0}

    async def _call(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        if fail_marker and fail_marker in task:
            raise RuntimeError(f"spawn rejected for {fail_marker}")
        state["active"] += 1
        state["calls"] += 1
        index = state["calls"]
        peak["value"] = max(peak["value"], state["active"])
        try:
            if delay:
                await asyncio.sleep(delay)
            return f"agent:auto:subagent:{index}"
        finally:
            state["active"] -= 1

    _call.state = state  # type: ignore[attr-defined]
    return _call


@pytest.mark.asyncio
async def test_fan_out_persists_every_child_key(isolated_db: Path, monkeypatch):
    """Three dependents unlocked at once: three distinct keys in the final state."""
    await _seed_flow_with_fanout("flow-wave", dependents=3)
    peak = {"value": 0}
    fake = _tracking_dispatch(peak, delay=0.01)
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    # Raise the cap above the wave size: the whole wave runs at once, so the
    # peak is what proves concurrency (the cap itself is pinned separately).
    monkeypatch.setitem(TASKFLOW_INFRA, "max_parallel_dispatch", 3)

    out = await taskflow_resume.coroutine(
        flow_id="flow-wave",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    assert out.startswith("TaskFlow resumed"), out
    flow = await store_sqlite.get_flow("flow-wave", _SESSION)
    assert flow is not None
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    keys = [by_id[f"step-{i}"]["child_session_key"] for i in (1, 2, 3)]
    assert all(keys), by_id
    assert len(set(keys)) == 3
    assert all(by_id[f"step-{i}"]["status"] == "dispatched" for i in (1, 2, 3))
    # The wave really was concurrent (a sequential loop would peak at 1).
    assert peak["value"] == 3


@pytest.mark.asyncio
async def test_the_wave_respects_max_parallel_dispatch(isolated_db: Path, monkeypatch):
    """With the cap at 2, at most two spawns are ever in flight."""
    await _seed_flow_with_fanout("flow-cap", dependents=3)
    peak = {"value": 0}
    fake = _tracking_dispatch(peak, delay=0.01)
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    monkeypatch.setitem(TASKFLOW_INFRA, "max_parallel_dispatch", 2)

    out = await taskflow_resume.coroutine(
        flow_id="flow-cap",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    assert out.startswith("TaskFlow resumed"), out
    assert peak["value"] == 2  # bounded, and the bound was actually reached


@pytest.mark.asyncio
async def test_a_cap_of_one_is_the_sequential_behaviour(isolated_db: Path, monkeypatch):
    """``max_parallel_dispatch = 1`` restores the historical one-at-a-time loop."""
    await _seed_flow_with_fanout("flow-serial", dependents=3)
    peak = {"value": 0}
    fake = _tracking_dispatch(peak, delay=0.01)
    monkeypatch.setattr(_dispatch, "dispatch_child", fake)
    monkeypatch.setitem(TASKFLOW_INFRA, "max_parallel_dispatch", 1)

    out = await taskflow_resume.coroutine(
        flow_id="flow-serial",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    assert out.startswith("TaskFlow resumed"), out
    assert peak["value"] == 1
    flow = await store_sqlite.get_flow("flow-serial", _SESSION)
    assert flow is not None
    keys = [
        s.get("child_session_key") for s in flow["state"]["steps"] if s["step_id"] != "step-root"
    ]
    assert len(set(keys)) == 3 and all(keys)
