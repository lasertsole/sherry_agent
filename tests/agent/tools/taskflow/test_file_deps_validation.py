"""`file_deps` is an optional step field with a strict shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools._shared import new_step, validate_steps_list
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_run_task import taskflow_run_task

pytestmark = [pytest.mark.unit]

_SESSION = "sess-file-deps-run"
_FLOW = "flow-file-deps-run"


def _step(**extra) -> dict:
    return {
        "step_id": "step-1",
        "task": "verify alpha",
        "depends_on": [],
        "status": "ready",
        **extra,
    }


def test_new_step_keeps_file_deps():
    step = new_step("step-1", "verify alpha", file_deps=["alpha.py", "src/core.py"])

    assert step["file_deps"] == ["alpha.py", "src/core.py"]


def test_absent_file_deps_stays_absent():
    step = new_step("step-1", "verify alpha")

    assert "file_deps" not in step


def test_file_deps_must_be_a_list():
    error = validate_steps_list([_step(file_deps="alpha.py")])

    assert error is not None and "file_deps must be a list" in error


def test_file_deps_entries_must_be_non_empty_strings():
    error = validate_steps_list([_step(file_deps=["alpha.py", " "])])

    assert error is not None and "invalid file_deps entry" in error


def test_an_empty_file_deps_list_is_valid():
    assert validate_steps_list([_step(file_deps=[])]) is None


async def _blocked_flow() -> None:
    await taskflow_create.coroutine(
        session_id=_SESSION,
        flow_id=_FLOW,
        description="file_deps run_task probe",
        initial_state={
            "steps": [
                {
                    "step_id": "step-1",
                    "task": "first",
                    "depends_on": [],
                    "status": "ready",
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_run_task_stores_file_deps_on_a_blocked_step(isolated_db: Path):
    await _blocked_flow()

    out = await taskflow_run_task.coroutine(
        flow_id=_FLOW,
        task="verify alpha",
        depends_on=["step-1"],
        file_deps=["alpha.py"],
        session_id=_SESSION,
    )

    assert "blocked" in out
    flow = await store_sqlite.get_flow(_FLOW, _SESSION)
    assert flow is not None
    assert flow["state"]["steps"][1]["file_deps"] == ["alpha.py"]


@pytest.mark.asyncio
async def test_run_task_rejects_malformed_file_deps(isolated_db: Path):
    await _blocked_flow()

    out = await taskflow_run_task.coroutine(
        flow_id=_FLOW,
        task="verify alpha",
        file_deps="alpha.py",
        session_id=_SESSION,
    )

    assert "file_deps must be a list of non-empty strings" in out
    flow = await store_sqlite.get_flow(_FLOW, _SESSION)
    assert flow is not None
    assert len(flow["state"]["steps"]) == 1


@pytest.mark.parametrize(
    "depends",
    [
        # two-node cycle
        {"step-1": ["step-2"], "step-2": ["step-1"]},
        # self-reachable cycle through a chain (self-edge is caught earlier)
        {"step-1": ["step-3"], "step-2": ["step-1"], "step-3": ["step-2"]},
    ],
)
def test_a_dependency_cycle_is_refused(depends: dict[str, list[str]]):
    """P3-9: a cycle must be refused at creation, not discovered by a dead scheduler.

    ``depends_on`` is what unlocks a step, so a cycle means no member ever
    becomes ready: the flow would sit active until the inactivity sweep fails
    it — the plan is malformed, and the answer names the loop.
    """
    steps = [
        {"step_id": sid, "task": f"do {sid}", "depends_on": deps, "status": "ready"}
        for sid, deps in depends.items()
    ]

    error = validate_steps_list(steps)

    assert error is not None and error.startswith("Error: dependency cycle")
    assert "step-1" in error


def test_a_diamond_is_not_a_cycle():
    """Shared prerequisites are the normal DAG shape and must stay legal."""
    steps = [
        {"step_id": "a", "task": "a", "depends_on": [], "status": "ready"},
        {"step_id": "b", "task": "b", "depends_on": ["a"], "status": "ready"},
        {"step_id": "c", "task": "c", "depends_on": ["a"], "status": "ready"},
        {"step_id": "d", "task": "d", "depends_on": ["b", "c"], "status": "ready"},
    ]

    assert validate_steps_list(steps) is None
