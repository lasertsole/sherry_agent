"""A wave unlocked by two concurrent completes spawns each step exactly once.

Both resumes read the pre-unlock state, so both believe they unlocked the
dependent; the CAS write serializes them and the persisted state is what the
wave is derived from — the loser's ``unlock_dependents`` runs against a fresh
step list where the dependent is already ``ready`` and returns nothing. Without
that, one logical step would get two live children (double work, two completions
racing to resume the same step).
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-no-double"
_CHILD_A = "agent:main:subagent:nd-a"
_CHILD_B = "agent:main:subagent:nd-b"


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


async def _seed(flow_id: str) -> None:
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="double dispatch probe",
        initial_state={
            "steps": [
                _step("step-a", "dispatched", child=_CHILD_A),
                _step("step-b", "dispatched", child=_CHILD_B),
                # Both A and B must be done before this one runs.
                _step("step-c", "blocked", depends_on=["step-a", "step-b"]),
            ]
        },
    )
    assert "Error" not in out, out


@pytest.mark.asyncio
async def test_two_resumes_unlock_the_dependent_once(isolated_db: Path, monkeypatch):
    await _seed("flow-nd")
    spawned: list[str] = []

    async def _fake(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        spawned.append(task)
        return "agent:auto:subagent:only-one"

    monkeypatch.setattr(_dispatch, "dispatch_child", _fake)

    out_a, out_b = await asyncio.gather(
        taskflow_resume.coroutine(
            flow_id="flow-nd",
            child_session_key=_CHILD_A,
            result="a done",
            session_id=_SESSION,
        ),
        taskflow_resume.coroutine(
            flow_id="flow-nd",
            child_session_key=_CHILD_B,
            result="b done",
            session_id=_SESSION,
        ),
    )

    assert out_a.startswith("TaskFlow resumed"), out_a
    assert out_b.startswith("TaskFlow resumed"), out_b
    # Exactly one spawn for step-c, whichever resume did the unlocking.
    assert len(spawned) == 1, spawned
    flow = await store_sqlite.get_flow("flow-nd", _SESSION)
    assert flow is not None
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert by_id["step-c"]["status"] == "dispatched"
    assert by_id["step-c"]["child_session_key"] == "agent:auto:subagent:only-one"
    counts = {s["step_id"]: s["status"] for s in flow["state"]["steps"]}
    assert counts == {"step-a": "done", "step-b": "done", "step-c": "dispatched"}


@pytest.mark.asyncio
async def test_a_second_dispatch_of_a_running_step_is_refused(isolated_db: Path, monkeypatch):
    """The manual door agrees: a dispatched step cannot be dispatched again."""
    from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch

    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-nd2",
        description="manual dispatch probe",
        initial_state={
            "steps": [
                _step("step-a", "ready"),
                _step("step-b", "ready"),
            ]
        },
    )
    assert "Error" not in out, out
    spawned: list[str] = []

    async def _fake(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        spawned.append(task)
        return "agent:auto:subagent:first"

    monkeypatch.setattr(_dispatch, "dispatch_child", _fake)

    first = await taskflow_dispatch.coroutine(
        flow_id="flow-nd2", step_ids=["step-a", "step-b"], session_id=_SESSION
    )

    assert first.startswith("TaskFlow steps dispatched"), first
    assert len(spawned) == 2

    # A second dispatch of the same ids is refused BEFORE any spawn.
    second = await taskflow_dispatch.coroutine(
        flow_id="flow-nd2", step_ids=["step-a"], session_id=_SESSION
    )

    assert second.startswith("Error:"), second
    assert "not dispatchable" in second
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_two_concurrent_manual_dispatches_spawn_once(isolated_db: Path, monkeypatch):
    """The flow-local lock serializes two manual dispatchers on the same step."""
    from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch

    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id="flow-nd3",
        description="concurrent manual dispatch probe",
        initial_state={"steps": [_step("step-a", "ready")]},
    )
    assert "Error" not in out, out
    spawned: list[str] = []

    async def _fake(task: str, requester_session_key: str, label=None, **_kwargs) -> str:
        # The delay widens the pre-spawn window: without the lock both callers
        # would pass the ready check and spawn twice.
        await asyncio.sleep(0.05)
        spawned.append(task)
        return "agent:auto:subagent:manual-once"

    monkeypatch.setattr(_dispatch, "dispatch_child", _fake)

    first, second = await asyncio.gather(
        taskflow_dispatch.coroutine(flow_id="flow-nd3", step_ids=["step-a"], session_id=_SESSION),
        taskflow_dispatch.coroutine(flow_id="flow-nd3", step_ids=["step-a"], session_id=_SESSION),
    )

    outcomes = (first, second)
    assert sum(out.startswith("TaskFlow steps dispatched") for out in outcomes) == 1, outcomes
    assert sum(out.startswith("Error:") for out in outcomes) == 1, outcomes
    assert len(spawned) == 1, spawned
    flow = await store_sqlite.get_flow("flow-nd3", _SESSION)
    assert flow is not None
    step = next(s for s in flow["state"]["steps"] if s["step_id"] == "step-a")
    assert step["status"] == "dispatched"
    assert step["child_session_key"] == "agent:auto:subagent:manual-once"
