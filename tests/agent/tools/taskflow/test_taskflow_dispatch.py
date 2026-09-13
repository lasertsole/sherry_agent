"""Behavior tests for taskflow_dispatch (todo 3 of taskflow-dag-phase1).

These were written BEFORE the implementation (red -> green) and pin the batch
assignment contract without the LLM or the real spawn pipeline. The seam is
``agent.tools.taskflow.tools._dispatch.dispatch_child`` and is monkeypatched in
every test here.

Coverage (mirrors the plan's acceptance criteria):
(a) two ``ready`` steps dispatch in ONE call and both become ``dispatched``
(b) a still-``blocked`` id is rejected with Error and zero spawns
(c) an already-``dispatched`` id is rejected
(d) an unknown id is rejected
(e) a mid-batch spawn failure persists the first success and reports the failure
(f) the flow-level ``child_session_key`` is unchanged
(g) a ``blocked`` step whose deps are now satisfied IS dispatchable
plus: empty ``step_ids``, duplicate ids, not-found and terminal flow guards.
"""

from pathlib import Path

import pytest

from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import _dispatch
from agent.tools.taskflow.tools.taskflow_cancel import taskflow_cancel
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch

pytestmark = [pytest.mark.unit]

_SESSION = "sess-1"


def _step(
    step_id: str,
    status: StepStatus | str,
    *,
    task: str | None = None,
    child: str | None = None,
    depends_on: list[str] | None = None,
) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": task or f"task-{step_id}",
        "depends_on": list(depends_on or []),
        "status": str(status),
    }
    if child is not None:
        step["child_session_key"] = child
    return step


async def _seed(flow_id: str, steps: list[dict]) -> None:
    out = await taskflow_create.coroutine(
        flow_id=flow_id, description="dispatch probe", initial_state={"steps": steps}
    )
    assert "Error" not in out


def _recording_dispatch(calls: list, keys: list[str], fail_at: int | None = None):
    """Fake seam: records calls; raises RuntimeError on the Nth (1-based) call."""

    async def _fake(task: str, requester_session_key: str, label: str | None = None) -> str:
        calls.append((task, requester_session_key, label))
        index = len(calls)
        if fail_at is not None and index == fail_at:
            raise RuntimeError(f"spawn rejected for call {index}")
        return keys[index - 1]

    return _fake


async def _steps_map(flow_id: str) -> dict[str, dict]:
    flow = await store_sqlite.get_flow(flow_id)
    assert flow is not None
    return {s["step_id"]: s for s in flow["state"]["steps"]}


# ---------------------------------------------------------------------------
# (a) two ready steps dispatch in one call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_ready_steps_dispatch_in_one_call(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        _dispatch,
        "dispatch_child",
        _recording_dispatch(
            calls,
            ["agent:main:subagent:child-1", "agent:main:subagent:child-2"],
        ),
    )
    await _seed("flow-1", [_step("step-1", StepStatus.READY), _step("step-2", StepStatus.READY)])

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    assert "Error" not in out, out
    assert len(calls) == 2
    assert [c[0] for c in calls] == ["task-step-1", "task-step-2"]
    steps = await _steps_map("flow-1")
    assert steps["step-1"]["status"] == StepStatus.DISPATCHED
    assert steps["step-2"]["status"] == StepStatus.DISPATCHED
    assert steps["step-1"]["child_session_key"] == "agent:main:subagent:child-1"
    assert steps["step-2"]["child_session_key"] == "agent:main:subagent:child-2"
    assert steps["step-1"]["dispatched_at"] > 0
    assert steps["step-2"]["dispatched_at"] > 0


# ---------------------------------------------------------------------------
# (b) still-blocked id rejected: all-or-nothing, zero spawns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_still_blocked_id_rejected_with_zero_spawns(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch(calls, ["unused"]))
    await _seed(
        "flow-1",
        [
            _step("step-1", StepStatus.DISPATCHED, child="agent:main:subagent:child-1"),
            _step("step-2", StepStatus.BLOCKED, depends_on=["step-1"]),
            _step("step-3", StepStatus.READY),
        ],
    )

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-3", "step-2"], session_id=_SESSION
    )

    assert out.startswith("Error"), out
    assert "step-2" in out
    assert calls == []
    steps = await _steps_map("flow-1")
    assert steps["step-3"]["status"] == StepStatus.READY
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["expected_revision"] == 1  # no state change


# ---------------------------------------------------------------------------
# (c) already-dispatched id rejected: no spawn at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_dispatched_id_rejected_without_spawning(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch(calls, ["unused"]))
    await _seed(
        "flow-1",
        [
            _step("step-1", StepStatus.DISPATCHED, child="agent:main:subagent:child-1"),
            _step("step-2", StepStatus.READY),
        ],
    )

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-2", "step-1"], session_id=_SESSION
    )

    assert out.startswith("Error"), out
    assert "step-1" in out
    assert calls == []
    steps = await _steps_map("flow-1")
    assert steps["step-2"]["status"] == StepStatus.READY


# ---------------------------------------------------------------------------
# (d) unknown id rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_id_rejected(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch(calls, ["unused"]))
    await _seed("flow-1", [_step("step-1", StepStatus.READY)])

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "ghost"], session_id=_SESSION
    )

    assert out.startswith("Error"), out
    assert "ghost" in out
    assert calls == []


# ---------------------------------------------------------------------------
# (e) mid-batch spawn failure: first success persisted, failure reported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_batch_spawn_failure_persists_first_success(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        _dispatch,
        "dispatch_child",
        _recording_dispatch(calls, ["agent:main:subagent:child-1"], fail_at=2),
    )
    await _seed("flow-1", [_step("step-1", StepStatus.READY), _step("step-2", StepStatus.READY)])

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-2"], session_id=_SESSION
    )

    assert out.startswith("Error"), out
    assert "step-1" in out  # reported as dispatched
    assert "step-2" in out  # reported as failed
    assert len(calls) == 2  # both attempts observed; no hidden extra spawn

    # The successful spawn is recorded; the failed one is not.
    steps = await _steps_map("flow-1")
    assert steps["step-1"]["status"] == StepStatus.DISPATCHED
    assert steps["step-1"]["child_session_key"] == "agent:main:subagent:child-1"
    assert steps["step-2"]["status"] == StepStatus.READY
    assert "child_session_key" not in steps["step-2"]


# ---------------------------------------------------------------------------
# (f) flow-level child_session_key is untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_level_child_session_key_unchanged(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        _dispatch,
        "dispatch_child",
        _recording_dispatch(calls, ["agent:main:subagent:child-1"]),
    )
    await _seed("flow-1", [_step("step-1", StepStatus.READY)])
    before = await store_sqlite.get_flow("flow-1")
    assert before is not None
    assert before["child_session_key"] is None

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )

    assert "Error" not in out, out
    after = await store_sqlite.get_flow("flow-1")
    assert after is not None
    assert after["child_session_key"] is None  # per-step keys are authoritative


# ---------------------------------------------------------------------------
# (g) blocked step with satisfied deps IS dispatchable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_step_with_satisfied_deps_is_dispatchable(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        _dispatch,
        "dispatch_child",
        _recording_dispatch(calls, ["agent:main:subagent:child-2"]),
    )
    await _seed(
        "flow-1",
        [
            _step("step-1", StepStatus.DONE, child="agent:main:subagent:child-1"),
            _step("step-2", StepStatus.BLOCKED, depends_on=["step-1"]),
        ],
    )

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-2"], session_id=_SESSION
    )

    assert "Error" not in out, out
    assert len(calls) == 1
    steps = await _steps_map("flow-1")
    assert steps["step-2"]["status"] == StepStatus.DISPATCHED
    assert steps["step-2"]["child_session_key"] == "agent:main:subagent:child-2"


# ---------------------------------------------------------------------------
# Guards: empty ids, duplicates, not found, terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_step_ids_rejected(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch(calls, ["unused"]))
    await _seed("flow-1", [_step("step-1", StepStatus.READY)])

    out = await taskflow_dispatch.coroutine(flow_id="flow-1", step_ids=[], session_id=_SESSION)

    assert out == "Error: step_ids is required"
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_step_ids_rejected(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch(calls, ["unused"]))
    await _seed("flow-1", [_step("step-1", StepStatus.READY)])

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1", "step-1"], session_id=_SESSION
    )

    assert out.startswith("Error"), out
    assert calls == []
    steps = await _steps_map("flow-1")
    assert steps["step-1"]["status"] == StepStatus.READY


@pytest.mark.asyncio
async def test_unknown_flow_errors(isolated_db: Path):
    out = await taskflow_dispatch.coroutine(
        flow_id="ghost", step_ids=["step-1"], session_id=_SESSION
    )
    assert "not found" in out


@pytest.mark.asyncio
async def test_terminal_flow_errors(isolated_db: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_dispatch, "dispatch_child", _recording_dispatch(calls, ["unused"]))
    await _seed("flow-1", [_step("step-1", StepStatus.READY)])
    await taskflow_cancel.coroutine(flow_id="flow-1", reason="done")

    out = await taskflow_dispatch.coroutine(
        flow_id="flow-1", step_ids=["step-1"], session_id=_SESSION
    )

    assert "terminal" in out
    assert calls == []
