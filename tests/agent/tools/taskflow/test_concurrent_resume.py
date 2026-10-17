"""Concurrent resumes of the same flow must never lose a result.

Two children of one flow finish at the same moment and their completion
carriers deliver two ``taskflow_resume`` calls that overlap: both read the
same revision, both write. The loser of the optimistic-lock race used to
return a bare ``Error: revision conflict`` — the parent's result was dropped
and the step list kept the other completion only. The resume path now retries
through ``update_flow_with_conflict_retry``: the callback re-applies the
ALREADY-DECIDED verdict + result onto the freshly-read state, so both results
land and the unlock set is derived from what was actually persisted.
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.registry.store_sqlite import FlowConflictError
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume

pytestmark = [pytest.mark.unit]

_SESSION = "sess-concurrent"
_CHILD_A = "agent:main:subagent:resume-a"
_CHILD_B = "agent:main:subagent:resume-b"


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


async def _seed(flow_id: str, steps: list[dict]) -> None:
    out = await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="concurrent resume probe",
        initial_state={"steps": steps},
    )
    assert "Error" not in out, out


async def _flow(flow_id: str) -> dict:
    flow = await store_sqlite.get_flow(flow_id, _SESSION)
    assert flow is not None
    return flow


@pytest.mark.asyncio
async def test_two_concurrent_resumes_both_land(isolated_db: Path):
    """Two overlapping resume calls: both results present, dependents unlocked once."""
    await _seed(
        "flow-1",
        [
            _step("step-1", "dispatched", child=_CHILD_A),
            _step("step-2", "dispatched", child=_CHILD_B),
            _step("step-3", "blocked", depends_on=["step-1", "step-2"]),
        ],
    )

    out_a, out_b = await asyncio.gather(
        taskflow_resume.coroutine(
            flow_id="flow-1",
            child_session_key=_CHILD_A,
            result="result-a",
            session_id=_SESSION,
            auto_dispatch=False,
        ),
        taskflow_resume.coroutine(
            flow_id="flow-1",
            child_session_key=_CHILD_B,
            result="result-b",
            session_id=_SESSION,
            auto_dispatch=False,
        ),
    )

    assert out_a.startswith("TaskFlow resumed"), out_a
    assert out_b.startswith("TaskFlow resumed"), out_b

    flow = await _flow("flow-1")
    state = flow["state"]
    injected = {r["child_session_key"]: r["result"] for r in state["results"]}
    assert injected == {_CHILD_A: "result-a", _CHILD_B: "result-b"}

    by_id = {s["step_id"]: s for s in state["steps"]}
    assert by_id["step-1"]["status"] == "done"
    assert by_id["step-2"]["status"] == "done"
    # Both dependencies done in the persisted state, so step-3 is unlocked.
    assert by_id["step-3"]["status"] == "ready"


@pytest.mark.asyncio
async def test_a_lost_race_does_not_drop_the_result(isolated_db: Path, monkeypatch):
    """Deterministic interleaving: the first write conflicts, the retry persists it."""
    await _seed(
        "flow-2",
        [
            _step("step-1", "dispatched", child=_CHILD_A),
            _step("step-2", "dispatched", child=_CHILD_B),
        ],
    )

    real_update = store_sqlite.update_flow
    fired = {"done": False}

    async def _wrapper(flow_id: str, expected_revision: int, **kwargs):
        if not fired["done"]:
            fired["done"] = True
            # A genuine concurrent writer bumps the revision first (as a
            # parallel resume's persist would), then this write loses the race.
            fresh = await store_sqlite.get_flow(flow_id, _SESSION)
            assert fresh is not None
            await real_update(
                flow_id,
                fresh["expected_revision"],
                session_id=_SESSION,
                total_tokens=777,
            )
            raise FlowConflictError(flow_id, expected_revision, expected_revision + 1)
        return await real_update(flow_id, expected_revision, **kwargs)

    monkeypatch.setattr(store_sqlite, "update_flow", _wrapper)

    out = await taskflow_resume.coroutine(
        flow_id="flow-2",
        child_session_key=_CHILD_A,
        result="survives the race",
        session_id=_SESSION,
        auto_dispatch=False,
    )

    assert out.startswith("TaskFlow resumed"), out
    assert fired["done"] is True  # the conflict genuinely fired; the retry persisted
    flow = await _flow("flow-2")
    assert flow["state"]["results"][-1]["result"] == "survives the race"
    # The concurrent writer's own change is intact too (no last-write-wins).
    assert flow["total_tokens"] == 777
    by_id = {s["step_id"]: s for s in flow["state"]["steps"]}
    assert by_id["step-1"]["status"] == "done"


@pytest.mark.asyncio
async def test_a_duplicate_delivery_still_short_circuits(isolated_db: Path):
    """The idempotency guard answers before any write, concurrent or not."""
    await _seed("flow-3", [_step("step-1", "dispatched", child=_CHILD_A)])

    first = await taskflow_resume.coroutine(
        flow_id="flow-3", child_session_key=_CHILD_A, result="once", session_id=_SESSION
    )
    assert first.startswith("TaskFlow resumed"), first
    revision_after_first = (await _flow("flow-3"))["expected_revision"]

    again = await taskflow_resume.coroutine(
        flow_id="flow-3", child_session_key=_CHILD_A, result="once", session_id=_SESSION
    )

    assert again.startswith("TaskFlow resume skipped"), again
    assert (await _flow("flow-3"))["expected_revision"] == revision_after_first
