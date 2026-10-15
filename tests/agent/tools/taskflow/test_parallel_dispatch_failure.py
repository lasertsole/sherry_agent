"""A partially failing wave persists every successful child and reports the rest.

The batch spawns concurrently, so a failure must not abort its siblings the way
the old sequential loop's early break did: the already-spawned children are
live, and dropping them from the state (or the answer) would strand real work.
Both doors are pinned — the automatic wave inside ``taskflow_resume`` and the
manual ``taskflow_dispatch`` batch.
"""

from pathlib import Path

import pytest

from config.features import TASKFLOW_INFRA

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-partial"
_ROOT_CHILD = "agent:main:subagent:pf-root"


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


async def _seed_fanout(flow_id: str, count: int) -> None:
    steps = [_step("step-root", "dispatched", child=_ROOT_CHILD)]
    for index in range(1, count + 1):
        steps.append(_step(f"step-{index}", "blocked", depends_on=["step-root"]))
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="partial failure probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


@pytest.mark.asyncio
async def test_auto_wave_persists_the_survivors_and_names_the_rest(isolated_db: Path, monkeypatch):
    """5 dependents, 2 spawns fail: 3 keys persisted, both failures in the answer."""
    await _seed_fanout("flow-pf", count=5)
    monkeypatch.setitem(TASKFLOW_INFRA, "max_parallel_dispatch", 3)
    spawned: list[str] = []

    async def _fake(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        # Fail on two known steps; every other step gets its own key.
        for failed in ("task-step-4", "task-step-5"):
            if failed in task:
                raise RuntimeError(f"spawn rejected for {failed}")
        spawned.append(task)
        return f"agent:auto:subagent:{len(spawned)}"

    monkeypatch.setattr(_dispatch, "dispatch_child", _fake)

    out = await taskflow_resume.coroutine(
        flow_id="flow-pf",
        child_session_key=_ROOT_CHILD,
        result="root done",
        session_id=_SESSION,
    )

    assert out.startswith("TaskFlow resumed"), out
    assert "auto_dispatch_failed=" in out
    assert "step-4" in out and "step-5" in out
    assert len(spawned) == 3

    flow = await store_sqlite.get_flow("flow-pf", _SESSION)
    assert flow is not None
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    survivors = [f"step-{i}" for i in (1, 2, 3)]
    for sid in survivors:
        assert by_id[sid]["status"] == "dispatched"
        assert by_id[sid]["child_session_key"].startswith("agent:auto:subagent:")
    assert len({by_id[sid]["child_session_key"] for sid in survivors}) == 3
    # The failed steps stay ready for a manual retry — never marked dispatched.
    for sid in ("step-4", "step-5"):
        assert by_id[sid]["status"] == "ready"
        assert "child_session_key" not in by_id[sid]


async def _seed_ready(flow_id: str, count: int) -> None:
    steps = [_step(f"step-{index}", "ready") for index in range(1, count + 1)]
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="manual batch probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


@pytest.mark.asyncio
async def test_manual_batch_persists_the_survivors_and_names_the_rest(
    isolated_db: Path, monkeypatch
):
    await _seed_ready("flow-pf2", count=5)
    monkeypatch.setitem(TASKFLOW_INFRA, "max_parallel_dispatch", 5)
    spawned: list[str] = []

    async def _fake(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        if "task-step-2" in task:
            raise RuntimeError("registry refused")
        spawned.append(task)
        return f"agent:auto:subagent:{len(spawned)}"

    monkeypatch.setattr(_dispatch, "dispatch_child", _fake)

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-pf2",
        step_ids=["step-1", "step-2", "step-3", "step-4", "step-5"],
        session_id=_SESSION,
    )

    # A partial failure is an Error that STILL reports what was dispatched.
    assert out.startswith("Error:"), out
    assert "step-2" in out
    assert len(spawned) == 4
    flow = await store_sqlite.get_flow("flow-pf2", _SESSION)
    assert flow is not None
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    dispatched = [sid for sid, s in by_id.items() if s["status"] == "dispatched"]
    assert sorted(dispatched) == ["step-1", "step-3", "step-4", "step-5"]
    assert by_id["step-2"]["status"] == "ready"


@pytest.mark.asyncio
async def test_an_all_failed_wave_reports_every_id_and_persists_nothing(
    isolated_db: Path, monkeypatch
):
    await _seed_ready("flow-pf3", count=2)

    async def _fake(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        raise RuntimeError("registry down")

    monkeypatch.setattr(_dispatch, "dispatch_child", _fake)
    before = await store_sqlite.get_flow("flow-pf3", _SESSION)
    assert before is not None

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-pf3", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    assert out.startswith("Error:"), out
    assert "step-1" in out and "step-2" in out
    after = await store_sqlite.get_flow("flow-pf3", _SESSION)
    assert after is not None
    # Nothing spawned, so nothing was written — the revision is untouched.
    assert after["expected_revision"] == before["expected_revision"]
