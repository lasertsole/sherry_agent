"""Live-network e2e: ``taskflow_plan`` starts its initial ready set for real.

LIVE-NETWORK, RUN EXPLICITLY. The test asks the real TaskFlow planner for two
independent steps, calls the real ``taskflow_plan`` tool, and verifies that the
initial ready set was auto-dispatched through the real subagent spawn pipeline
(not the hermetic fake seam). The children are killed and every trace of the
session is purged in ``finally``.

The hermetic sibling coverage lives in
``tests/agent/tools/taskflow/test_taskflow_plan_auto_dispatch.py`` and
``test_taskflow_resume_all_ready.py``. Run this one with::

    uv run --no-sync pytest -m llm_e2e tests/full/test_taskflow_plan_auto_dispatch_e2e.py -v --durations=0
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from pathlib import Path

import pytest

import agent.core as agent_core
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.tools.taskflow.registry import store_sqlite as flow_store
from agent.tools.taskflow.tools.taskflow_plan import taskflow_plan
from config import SESSIONS_DIR
from context_engine import delete_messages_by_session
from runtime import clear_all_register_sessions

pytestmark = [pytest.mark.llm_e2e, pytest.mark.system, pytest.mark.timeout(600)]

_SESSION = f"e2e-plan-auto-{uuid.uuid4().hex[:8]}"
_FLOW_ID = "flow-e2e-plan-auto"


@pytest.fixture()
def flow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real TaskFlow + todo SQLite stores pointed at per-test tmp files."""
    db_dir = tmp_path / "taskflow"
    monkeypatch.setattr(flow_store, "_DB_DIR", db_dir)
    monkeypatch.setattr(flow_store, "_DB_PATH", db_dir / "taskflow_registry.db")
    monkeypatch.setattr(flow_store, "_initialized", False)
    monkeypatch.setattr(flow_store, "_init_loop", None)
    monkeypatch.setattr(flow_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(flow_store, "_sync_tables_ready", False)

    # taskflow_plan binds a flow-root todo at birth; keep it out of the repo's
    # real todos.db just like the hermetic taskflow fixture does.
    from agent.tools.todolist.registry import store_sqlite as todo_store

    monkeypatch.setattr(todo_store, "_DB_DIR", tmp_path)
    monkeypatch.setattr(todo_store, "_DB_PATH", tmp_path / "todos.db")
    monkeypatch.setattr(todo_store, "_initialized", False)
    monkeypatch.setattr(todo_store, "_init_loop", None)
    monkeypatch.setattr(todo_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(todo_store, "_sync_tables_ready", False)
    return db_dir


@pytest.fixture()
def clean_subagent_registry():
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


async def _purge_session(session_id: str) -> None:
    """Delete every trace of the session this test created (best effort)."""
    from agent.tools.subagent.control.kill import kill_all_controlled_subagent_runs

    with contextlib.suppress(Exception):
        await kill_all_controlled_subagent_runs(session_id, reason="e2e_plan_cleanup")
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


@pytest.mark.asyncio
async def test_live_plan_auto_dispatches_initial_ready_siblings(
    flow_env: Path, clean_subagent_registry
) -> None:
    agent_core.init()
    try:
        out = await taskflow_plan.coroutine(
            goal=(
                "Answer two INDEPENDENT questions as two steps with NO depends_on between "
                "them: (a) 2+2 and (b) the capital of France. Each step answers directly "
                "and does not use tools."
            ),
            flow_id=_FLOW_ID,
            max_steps=2,
            session_id=_SESSION,
        )

        assert out.startswith("TaskFlow planned"), out
        assert "auto_dispatch: TaskFlow steps auto-dispatched" in out, out

        flow = await flow_store.get_flow(_FLOW_ID, _SESSION)
        assert flow is not None
        steps = list(flow["state"]["steps"])
        dispatched = [
            step
            for step in steps
            if step.get("status") == "dispatched" and step.get("child_session_key")
        ]
        assert dispatched, f"the initial ready set was not dispatched: {steps}"
        assert not [step for step in steps if step.get("status") == "ready"], (
            f"a ready step was left behind after auto-dispatch: {steps}"
        )

        from agent.tools.subagent.registry import get_run_by_child_session_key

        for step in dispatched:
            run = get_run_by_child_session_key(step["child_session_key"])
            assert run is not None, f"no real child run for {step['step_id']}"
            assert step["task"] in run.task, (
                f"child task does not match the step: {run.task!r} vs {step['task']!r}"
            )
    finally:
        await _purge_session(_SESSION)
