"""Behavior tests for taskflow_update_steps (dynamic DAG edits).

Covers the full-replace contract plus all safety rules:

(a) add / remove / rewrite-task / rewrite-depends_on / reorder in one call
(b) dispatched steps cannot be downgraded; child_session_key is immutable
(c) done steps keep task / depends_on / status / child_session_key
(d) depends_on must be self-consistent (no unknown ids, no self-dependency)
(e) terminal flows and cross-session access are rejected
(f) removing a dispatched step warns only while its child is still running
(g) expected_revision CAS rejects with the latest revision
(h) after the update, taskflow_dispatch schedules the new/ready steps

The orphan-check registry seam (``get_run_by_child_session_key`` /
``is_live_unended_run``) is monkeypatched in every test that removes a
dispatched step; the real subagent registry is never touched.
"""

import importlib
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import INITIAL_REVISION, StepStatus, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite

_SESSION = "sess-update"
_OTHER_SESSION = "sess-other"
FLOW = "flow-update"

# The family package re-exports the tool under the same name, which shadows the
# submodule for package-attribute traversal; importlib resolves the real module.
update_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_update_steps")
dispatch_mod = importlib.import_module("agent.tools.taskflow.tools._dispatch")

pytestmark = [pytest.mark.unit]


class _Run:
    """Minimal stand-in for a SubagentRunRecord (only liveness matters here)."""

    def __init__(self, key: str, live: bool) -> None:
        self.child_session_key = key
        self.live = live


def _step(
    step_id: str,
    task: str | None = None,
    depends_on: list[str] | None = None,
    status: StepStatus | str = StepStatus.READY,
    child: str | None = None,
) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": task if task is not None else f"task-{step_id}",
        "depends_on": list(depends_on or []),
        "status": status.value if isinstance(status, StepStatus) else status,
    }
    if child is not None:
        step["child_session_key"] = child
    return step


async def _seed(
    steps: list[dict],
    *,
    flow_id: str = FLOW,
    session: str = _SESSION,
    status: str = TaskFlowStatus.RUNNING.value,
) -> None:
    await store_sqlite.create_flow(
        flow_id,
        {"description": "update probe", "steps": list(steps), "results": []},
        session_id=session,
        status=status,
    )


async def _update(
    steps: list[dict],
    *,
    flow_id: str = FLOW,
    session: str = _SESSION,
    expected_revision: int | None = None,
) -> str:
    return await update_mod.taskflow_update_steps.coroutine(
        flow_id=flow_id,
        steps=steps,
        expected_revision=expected_revision,
        session_id=session,
    )


async def _stored_steps(flow_id: str = FLOW, session: str = _SESSION) -> list[dict]:
    flow = await store_sqlite.get_flow(flow_id, session)
    assert flow is not None
    return flow["state"]["steps"]


async def _stored_revision(flow_id: str = FLOW, session: str = _SESSION) -> int:
    flow = await store_sqlite.get_flow(flow_id, session)
    assert flow is not None
    return int(flow["expected_revision"])


def _patch_registry(monkeypatch: pytest.MonkeyPatch, runs: dict[str, _Run]) -> list[str]:
    """Replace the orphan-check seam; return the list of queried child keys."""
    queried: list[str] = []

    def fake_get_run(child_session_key: str) -> _Run | None:
        queried.append(child_session_key)
        return runs.get(child_session_key)

    def fake_is_live(run: _Run) -> bool:
        return bool(run.live)

    monkeypatch.setattr(update_mod, "get_run_by_child_session_key", fake_get_run)
    monkeypatch.setattr(update_mod, "is_live_unended_run", fake_is_live)
    return queried


# ---------------------------------------------------------------------------
# (a) full replacement: add / remove / rewrite / reorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_replace_add_remove_modify_reorder(isolated_db: Path):
    # Given a 3-step DAG (step-1 -> step-2; step-3 independent)
    await _seed(
        [
            _step("step-1"),
            _step("step-2", depends_on=["step-1"], status=StepStatus.BLOCKED),
            _step("step-3"),
        ]
    )

    # When the complete list is replaced: reorder + rewrite + add + drop
    out = await _update(
        [
            _step("step-3", task="rewritten three"),
            _step("step-1"),
            _step("step-4", depends_on=["step-3"]),
        ]
    )

    # Then the stored list is exactly the passed order and content
    assert "TaskFlow steps updated" in out
    assert "steps=3" in out
    assert "added=[step-4]" in out
    assert "removed=[step-2]" in out
    assert "revision=2" in out
    assert "Warning" not in out
    steps = await _stored_steps()
    assert [s["step_id"] for s in steps] == ["step-3", "step-1", "step-4"]
    assert steps[0]["task"] == "rewritten three"
    assert steps[2]["depends_on"] == ["step-3"]
    assert all(s["status"] == "ready" for s in steps)


@pytest.mark.asyncio
async def test_rewrite_task_of_ready_and_blocked_steps(isolated_db: Path):
    await _seed(
        [
            _step("step-1", task="old one"),
            _step("step-2", task="old two", depends_on=["step-1"], status=StepStatus.BLOCKED),
        ]
    )

    out = await _update(
        [
            _step("step-1", task="new one"),
            _step("step-2", task="new two", depends_on=["step-1"], status=StepStatus.BLOCKED),
        ]
    )

    assert "Error" not in out
    steps = await _stored_steps()
    assert steps[0]["task"] == "new one"
    assert steps[1]["task"] == "new two"
    assert steps[1]["depends_on"] == ["step-1"]
    assert steps[1]["status"] == "blocked"


@pytest.mark.asyncio
async def test_rewrite_depends_on_of_ready_step(isolated_db: Path):
    await _seed([_step("step-1"), _step("step-2")])

    out = await _update([_step("step-1"), _step("step-2", depends_on=["step-1"])])

    assert "Error" not in out
    steps = await _stored_steps()
    assert steps[1]["depends_on"] == ["step-1"]
    assert steps[1]["status"] == "ready"


@pytest.mark.asyncio
async def test_empty_full_replace_removes_all_steps(isolated_db: Path):
    await _seed([_step("step-1"), _step("step-2")])

    out = await _update([])

    assert "TaskFlow steps updated" in out
    assert "steps=0" in out
    assert "removed=[step-1,step-2]" in out
    assert await _stored_steps() == []


# ---------------------------------------------------------------------------
# (b) dispatched steps: no downgrade, immutable child_session_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", [StepStatus.READY.value, StepStatus.BLOCKED.value])
async def test_dispatched_step_cannot_be_downgraded(isolated_db: Path, bad_status: str):
    await _seed([_step("step-1", status=StepStatus.DISPATCHED, child="child-1")])

    out = await _update([_step("step-1", status=bad_status, child="child-1")])

    assert "Error" in out
    assert "cannot be downgraded" in out
    assert await _stored_revision() == INITIAL_REVISION
    steps = await _stored_steps()
    assert steps[0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_dispatched_task_rewrite_keeps_child_session_key(isolated_db: Path):
    """A dispatched step's task text may change; the child key is retained even
    when the replacement entry omits it."""
    await _seed([_step("step-1", task="old task", status=StepStatus.DISPATCHED, child="child-1")])

    out = await _update([_step("step-1", task="new task", status=StepStatus.DISPATCHED)])

    assert "TaskFlow steps updated" in out
    steps = await _stored_steps()
    assert steps[0]["task"] == "new task"
    assert steps[0]["status"] == "dispatched"
    assert steps[0]["child_session_key"] == "child-1"


@pytest.mark.asyncio
async def test_dispatched_step_rejects_changed_child_key(isolated_db: Path):
    await _seed([_step("step-1", status=StepStatus.DISPATCHED, child="child-1")])

    out = await _update([_step("step-1", status=StepStatus.DISPATCHED, child="child-2")])

    assert "Error" in out
    assert "child_session_key is immutable" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", [StepStatus.DISPATCHED.value, StepStatus.DONE.value])
async def test_new_step_rejects_fabricated_status(isolated_db: Path, bad_status: str):
    await _seed([])

    out = await _update([_step("step-new", status=bad_status, child="child-x")])

    assert "Error" in out
    assert "new step 'step-new' must be ready or blocked" in out
    assert await _stored_revision() == INITIAL_REVISION


# ---------------------------------------------------------------------------
# (c) done steps: task / depends_on / status frozen, key retained
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_step_task_change_rejected(isolated_db: Path):
    await _seed([_step("step-1", task="original", status=StepStatus.DONE, child="child-1")])

    out = await _update([_step("step-1", task="edited", status=StepStatus.DONE, child="child-1")])

    assert "Error" in out
    assert "task cannot be changed" in out
    assert await _stored_revision() == INITIAL_REVISION
    steps = await _stored_steps()
    assert steps[0]["task"] == "original"


@pytest.mark.asyncio
async def test_done_step_depends_on_change_rejected(isolated_db: Path):
    await _seed(
        [
            _step("step-0", status=StepStatus.DONE, child="child-0"),
            _step("step-1", status=StepStatus.DONE, child="child-1"),
        ]
    )

    out = await _update(
        [
            _step("step-0", status=StepStatus.DONE, child="child-0"),
            _step("step-1", status=StepStatus.DONE, depends_on=["step-0"], child="child-1"),
        ]
    )

    assert "Error" in out
    assert "depends_on cannot be changed" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_done_step_status_change_rejected(isolated_db: Path):
    await _seed([_step("step-1", status=StepStatus.DONE, child="child-1")])

    out = await _update([_step("step-1", status=StepStatus.READY)])

    assert "Error" in out
    assert "status cannot be changed" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_done_step_survives_full_replace_with_key_retained(isolated_db: Path):
    await _seed([_step("step-1", task="kept", status=StepStatus.DONE, child="child-1")])

    out = await _update([_step("step-1", task="kept", status=StepStatus.DONE)])

    assert "TaskFlow steps updated" in out
    steps = await _stored_steps()
    assert steps[0]["status"] == "done"
    assert steps[0]["child_session_key"] == "child-1"
    assert steps[0]["task"] == "kept"


# ---------------------------------------------------------------------------
# (d) self-consistency of the new list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_depends_on_rejected(isolated_db: Path):
    await _seed([_step("step-1")])

    out = await _update([_step("step-1"), _step("step-2", depends_on=["step-9"])])

    assert "Error" in out
    assert "references unknown step_id 'step-9'" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_duplicate_step_id_rejected(isolated_db: Path):
    await _seed([])

    out = await _update([_step("step-1"), _step("step-1", task="copy")])

    assert "Error" in out
    assert "duplicate step_id 'step-1'" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_self_dependency_rejected(isolated_db: Path):
    await _seed([])

    out = await _update([_step("step-1", depends_on=["step-1"])])

    assert "Error" in out
    assert "cannot depend on itself" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_invalid_status_rejected(isolated_db: Path):
    await _seed([])

    out = await _update([_step("step-1", status="zombie")])

    assert "Error" in out
    assert "invalid status" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_blank_task_rejected(isolated_db: Path):
    await _seed([])

    out = await _update([_step("step-1", task="   ")])

    assert "Error" in out
    assert "task is required" in out


# ---------------------------------------------------------------------------
# (e) terminal flows and session isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_flow_rejected(isolated_db: Path):
    await _seed([_step("step-1")], status=TaskFlowStatus.CANCELLED.value)

    out = await _update([_step("step-1", task="late edit")])

    assert "Error" in out
    assert "terminal" in out
    assert await _stored_revision() == INITIAL_REVISION


@pytest.mark.asyncio
async def test_cross_session_flow_rejected(isolated_db: Path):
    await _seed([_step("step-1")], session=_OTHER_SESSION)

    out = await _update([_step("step-1", task="foreign edit")], session=_SESSION)

    assert "Error" in out
    assert "not found" in out
    steps = await _stored_steps(session=_OTHER_SESSION)
    assert steps[0]["task"] == "task-step-1"


# ---------------------------------------------------------------------------
# (f) orphan-child warning on removing a dispatched step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_dispatched_step_warns_while_child_runs(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    queried = _patch_registry(monkeypatch, {"child-1": _Run("child-1", live=True)})
    await _seed(
        [
            _step("step-1", status=StepStatus.DISPATCHED, child="child-1"),
            _step("step-2"),
        ]
    )

    out = await _update([_step("step-2")])

    # Non-blocking: the write succeeded and the warning names the live child.
    assert "TaskFlow steps updated" in out
    assert "Warning" in out
    assert "removed step_id=step-1 still has a running child" in out
    assert "child-1" in out
    assert queried == ["child-1"]
    steps = await _stored_steps()
    assert [s["step_id"] for s in steps] == ["step-2"]


@pytest.mark.asyncio
async def test_removing_dispatched_step_silent_when_child_settled(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _patch_registry(monkeypatch, {"child-1": _Run("child-1", live=False)})
    await _seed([_step("step-1", status=StepStatus.DISPATCHED, child="child-1")])

    out = await _update([])

    assert "TaskFlow steps updated" in out
    assert "Warning" not in out


@pytest.mark.asyncio
async def test_removing_dispatched_step_silent_when_run_absent(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    _patch_registry(monkeypatch, {})
    await _seed([_step("step-1", status=StepStatus.DISPATCHED, child="child-1")])

    out = await _update([])

    assert "TaskFlow steps updated" in out
    assert "Warning" not in out


# ---------------------------------------------------------------------------
# (g) expected_revision CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expected_revision_mismatch_returns_latest(isolated_db: Path):
    await _seed([_step("step-1")])

    out = await _update([_step("step-1", task="edited")], expected_revision=99)

    assert "conflict" in out.lower()
    assert "latest revision=1" in out
    assert await _stored_revision() == INITIAL_REVISION
    steps = await _stored_steps()
    assert steps[0]["task"] == "task-step-1"


@pytest.mark.asyncio
async def test_expected_revision_match_succeeds(isolated_db: Path):
    await _seed([_step("step-1")])

    out = await _update([_step("step-1", task="edited")], expected_revision=INITIAL_REVISION)

    assert "TaskFlow steps updated" in out
    assert "revision=2" in out
    assert await _stored_revision() == 2


# ---------------------------------------------------------------------------
# (h) cooperation with the rest of the family + registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_then_dispatch_schedules_ready_step(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        return "child-new"

    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake_dispatch)
    await _seed([_step("step-1", status=StepStatus.DONE, child="child-1")])

    out = await _update(
        [
            _step("step-1", status=StepStatus.DONE, child="child-1"),
            _step("step-2", task="new step"),
        ]
    )
    assert "added=[step-2]" in out

    tools = {t.name: t for t in build_taskflow_tools()}
    dispatched = await tools["taskflow_dispatch"].coroutine(
        flow_id=FLOW, step_ids=["step-2"], session_id=_SESSION
    )

    assert "dispatched" in dispatched
    steps = await _stored_steps()
    assert steps[1]["step_id"] == "step-2"
    assert steps[1]["status"] == "dispatched"
    assert steps[1]["child_session_key"] == "child-new"


def test_update_steps_registered_with_main_only_metadata():
    tools = {t.name: t for t in build_taskflow_tools()}

    assert "taskflow_update_steps" in tools
    assert tools["taskflow_update_steps"].metadata == {
        "scope": "main_only",
        "idempotent": False,
    }


def test_update_steps_in_main_toolset(build_main_tools_real):
    names = [t.name for t in build_main_tools_real()]

    assert "taskflow_update_steps" in names
