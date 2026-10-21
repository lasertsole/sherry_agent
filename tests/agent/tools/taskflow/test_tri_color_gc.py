"""Tri-color orphan GC + liveness + entry binding (v3).

The stores under test are the real SQLite implementations, pointed at tmp files
by the taskflow conftest (which also isolates the todolist store).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.subagent.registry import sweeper
from agent.tools.taskflow.config import TABLE_NAME, TaskFlowStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_finish import taskflow_finish
from agent.tools.taskflow.tools.taskflow_plan import taskflow_plan
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.todolist.service import TodoService, TodoStoreError
from config.features import TASKFLOW_INFRA

pytestmark = [pytest.mark.unit]

SESSION = "sess-gc"
OTHER = "sess-gc-other"


def _state(description: str = "gc probe", steps: list[dict] | None = None) -> dict:
    return {"description": description, "steps": steps or [], "results": []}


async def _create(
    flow_id: str, *, session_id: str = SESSION, steps: list[dict] | None = None
) -> dict:
    return await store_sqlite.create_flow(
        flow_id, _state(f"{flow_id} probe", steps), session_id=session_id
    )


async def _todo(
    flow_id: str | None,
    *,
    session_id: str = SESSION,
    status: str = "pending",
    content: str = "root",
) -> None:
    await todo_store.replace_all(
        session_id,
        [{"content": content, "status": status, "flow_id": flow_id}],
    )


def _age(db_path: Path, flow_id: str, seconds_ago: float) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET updated_at = ? WHERE flow_id = ?",
            (time.time() - seconds_ago, flow_id),
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# store migration / clock / supersede
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gc_columns_migrate_a_legacy_database(isolated_db: Path):
    old_schema = f"""
    CREATE TABLE {TABLE_NAME} (
        flow_id TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        wait_json TEXT,
        expected_revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        child_session_key TEXT,
        total_tokens INTEGER DEFAULT 0,
        total_cost REAL DEFAULT 0.0,
        token_budget INTEGER DEFAULT 0,
        deadline_ts REAL,
        session_id TEXT NOT NULL DEFAULT '',
        resumed_from TEXT,
        superseded_by TEXT
    )
    """
    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute(old_schema)
        conn.execute(
            f"INSERT INTO {TABLE_NAME} (flow_id, state_json, expected_revision, status, session_id) "
            "VALUES ('legacy', '{}', 1, 'running', ?)",
            (SESSION,),
        )
        conn.commit()
    finally:
        conn.close()

    await store_sqlite.ensure_db()

    conn = sqlite3.connect(isolated_db)
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
        updated = conn.execute(
            f"SELECT updated_at FROM {TABLE_NAME} WHERE flow_id = 'legacy'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert {"updated_at", "terminal_reason"} <= columns
    assert updated is not None


@pytest.mark.asyncio
async def test_update_flow_bumps_updated_at_and_records_terminal_reason(isolated_db: Path):
    flow = await _create("flow-clock")
    before = flow["updated_at"]
    assert before is not None and flow["terminal_reason"] is None

    updated = await store_sqlite.update_flow(
        "flow-clock",
        flow["expected_revision"],
        session_id=SESSION,
        status=TaskFlowStatus.FAILED.value,
        terminal_reason="test_reason",
    )

    assert updated["updated_at"] >= before
    assert updated["terminal_reason"] == "test_reason"


@pytest.mark.asyncio
async def test_superseded_active_flow_becomes_cancelled_and_terminal(isolated_db: Path):
    await _create("flow-old")

    marked = await store_sqlite.mark_flow_superseded("flow-old", "flow-new", session_id=SESSION)

    assert marked is True
    old = await store_sqlite.get_flow("flow-old", SESSION)
    assert old is not None
    assert old["status"] == TaskFlowStatus.CANCELLED.value
    assert old["terminal_reason"] == "superseded"
    assert old["superseded_by"] == "flow-new"


# ---------------------------------------------------------------------------
# tri-color mark / sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tri_color_keeps_a_flow_with_an_active_todo(isolated_db: Path):
    await _create("flow-live")
    await _todo("flow-live")

    result = await sweeper._tri_color_sweep(now=_now() + 10_000, grace_seconds=0)

    assert result["marked"] == 1
    flow = await store_sqlite.get_flow("flow-live", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_tri_color_live_child_vetoes_a_deleted_root(isolated_db: Path, monkeypatch):
    await _create("flow-live-child")
    await _todo("flow-live-child")
    await todo_store.replace_all(SESSION, [])
    _age(isolated_db, "flow-live-child", 600)
    monkeypatch.setattr(sweeper, "_has_live_child", lambda flow: True)

    result = await sweeper._tri_color_sweep(now=_now(), grace_seconds=0)

    assert result["swept"] == 0
    flow = await store_sqlite.get_flow("flow-live-child", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_tri_color_collects_an_unreferenced_flow(isolated_db: Path):
    await _create(
        "flow-orphan",
        steps=[{"step_id": "s1", "task": "work", "status": "dispatched"}],
    )
    _age(isolated_db, "flow-orphan", 600)

    result = await sweeper._tri_color_sweep(now=_now(), grace_seconds=0)

    assert result["swept"] == 1
    flow = await store_sqlite.get_flow("flow-orphan", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.FAILED.value
    assert flow["terminal_reason"] == "orphan_unreferenced"
    assert flow["state"]["steps"][0]["status"] == "failed"
    assert flow["state"]["steps"][0]["fail_reason"] == "orphan_unreferenced"


@pytest.mark.asyncio
async def test_tri_color_grace_keeps_a_recent_unreferenced_flow(isolated_db: Path):
    await _create("flow-fresh")
    _age(isolated_db, "flow-fresh", 10)

    result = await sweeper._tri_color_sweep(now=_now(), grace_seconds=300)

    assert result["swept"] == 0
    flow = await store_sqlite.get_flow("flow-fresh", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_tri_color_ignores_a_terminal_todo_root(isolated_db: Path):
    await _create("flow-done-root")
    await _todo("flow-done-root", status="completed")
    _age(isolated_db, "flow-done-root", 600)

    result = await sweeper._tri_color_sweep(now=_now(), grace_seconds=0)

    assert result["swept"] == 1
    flow = await store_sqlite.get_flow("flow-done-root", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.FAILED.value


@pytest.mark.asyncio
async def test_tri_color_skips_terminal_and_superseded_flows(isolated_db: Path):
    await _create("flow-terminal")
    await store_sqlite.update_flow(
        "flow-terminal",
        1,
        session_id=SESSION,
        status=TaskFlowStatus.DONE.value,
        terminal_reason="completed",
    )
    await _create("flow-superseded")
    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET superseded_by = 'flow-heir' WHERE flow_id = 'flow-superseded'"
        )
        conn.commit()
    finally:
        conn.close()
    _age(isolated_db, "flow-terminal", 600)
    _age(isolated_db, "flow-superseded", 600)

    result = await sweeper._tri_color_sweep(now=_now(), grace_seconds=0)

    assert result["swept"] == 0
    terminal = await store_sqlite.get_flow("flow-terminal", SESSION)
    superseded = await store_sqlite.get_flow("flow-superseded", SESSION)
    assert terminal is not None and terminal["status"] == TaskFlowStatus.DONE.value
    assert superseded is not None and superseded["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_tri_color_is_session_scoped(isolated_db: Path):
    await _create("flow-a")
    await _todo("flow-a")
    await _create("flow-b", session_id=OTHER)
    _age(isolated_db, "flow-a", 600)
    _age(isolated_db, "flow-b", 600)

    await sweeper._tri_color_sweep(now=_now(), grace_seconds=0)

    alive = await store_sqlite.get_flow("flow-a", SESSION)
    dead = await store_sqlite.get_flow("flow-b", OTHER)
    assert alive is not None and alive["status"] == TaskFlowStatus.RUNNING.value
    assert dead is not None and dead["status"] == TaskFlowStatus.FAILED.value


# ---------------------------------------------------------------------------
# liveness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveness_collects_a_stale_flow_and_cancels_its_todo(isolated_db: Path):
    await _create("flow-stale")
    await _todo("flow-stale")
    _age(isolated_db, "flow-stale", 13 * 3600)

    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids={SESSION}, stale_action_hours=12
    )

    assert result["stale_swept"] == 1
    flow = await store_sqlite.get_flow("flow-stale", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.FAILED.value
    assert flow["terminal_reason"] == "stale_swept"
    todos = await todo_store.get_todos(SESSION)
    assert todos[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_liveness_marks_a_stale_flow_in_a_dead_session(isolated_db: Path):
    await _create("flow-dead")
    await _todo("flow-dead")
    _age(isolated_db, "flow-dead", 13 * 3600)

    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids=set(), stale_action_hours=12
    )

    assert result["session_dead"] == 1
    flow = await store_sqlite.get_flow("flow-dead", SESSION)
    assert flow is not None
    assert flow["terminal_reason"] == "session_dead"


@pytest.mark.asyncio
async def test_liveness_keeps_a_recent_flow(isolated_db: Path):
    await _create("flow-recent")
    _age(isolated_db, "flow-recent", 3600)

    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids={SESSION}, stale_action_hours=12
    )

    assert result["stale_swept"] == 0
    flow = await store_sqlite.get_flow("flow-recent", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_liveness_live_child_vetoes_collection(isolated_db: Path, monkeypatch):
    await _create("flow-child")
    _age(isolated_db, "flow-child", 13 * 3600)
    monkeypatch.setattr(sweeper, "_has_live_child", lambda flow: True)

    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids={SESSION}, stale_action_hours=12
    )

    assert result["stale_swept"] == 0
    flow = await store_sqlite.get_flow("flow-child", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.RUNNING.value


@pytest.mark.asyncio
async def test_child_session_flow_is_collected_after_the_short_grace(isolated_db: Path):
    child = "agent:main:subagent:child-gc"
    await _create("flow-child-dead", session_id=child)
    await _todo("flow-child-dead", session_id=child)
    _age(isolated_db, "flow-child-dead", 600)

    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids=set(), stale_action_hours=12
    )

    assert result["session_dead"] == 1
    flow = await store_sqlite.get_flow("flow-child-dead", child)
    assert flow is not None and flow["status"] == TaskFlowStatus.FAILED.value

    # A live child run (here: the session is in the active set) vetoes it.
    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids={child}, stale_action_hours=12
    )
    assert result["session_dead"] == 0


@pytest.mark.asyncio
async def test_liveness_cancels_the_todo_of_a_cancelled_flow(isolated_db: Path):
    await _create("flow-cancelled")
    await _todo("flow-cancelled")
    await store_sqlite.mark_flow_superseded("flow-cancelled", "flow-heir", session_id=SESSION)

    await sweeper._liveness_sweep(now=_now(), active_session_ids={SESSION})

    todos = await todo_store.get_todos(SESSION)
    assert todos[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_liveness_collects_a_stale_waiting_flow(isolated_db: Path):
    await _create("flow-waiting")
    await store_sqlite.update_flow(
        "flow-waiting",
        1,
        session_id=SESSION,
        status=TaskFlowStatus.WAITING.value,
        wait={"reason": "child"},
    )
    _age(isolated_db, "flow-waiting", 13 * 3600)

    result = await sweeper._liveness_sweep(
        now=_now(), active_session_ids={SESSION}, stale_action_hours=12
    )

    assert result["stale_swept"] == 1
    flow = await store_sqlite.get_flow("flow-waiting", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.FAILED.value


# ---------------------------------------------------------------------------
# entry defense + finish budgets + prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_auto_binds_a_flow_level_todo(isolated_db: Path):
    out = await taskflow_create.coroutine(
        session_id=SESSION,
        flow_id="flow-bind",
        description="bind probe",
        todo_content="检查内存泄漏",
    )

    assert "TaskFlow created" in out
    assert "todo bound: 检查内存泄漏" in out
    todos = await todo_store.get_todos(SESSION)
    assert len(todos) == 1
    assert todos[0]["status"] == "pending"
    assert todos[0]["flow_id"] == "flow-bind"
    assert todos[0]["step_id"] is None


@pytest.mark.asyncio
async def test_create_falls_back_to_the_description_for_the_root(isolated_db: Path):
    await taskflow_create.coroutine(
        session_id=SESSION, flow_id="flow-fallback", description="fallback probe"
    )

    todos = await todo_store.get_todos(SESSION)
    assert todos[0]["content"] == "fallback probe"
    assert todos[0]["flow_id"] == "flow-fallback"


@pytest.mark.asyncio
async def test_create_bind_failure_compensates_the_flow(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    async def _fail(*_args, **_kwargs):
        raise RuntimeError("todo store down")

    create_mod = importlib.import_module("agent.tools.taskflow.tools.taskflow_create")
    monkeypatch.setattr(create_mod, "bind_flow_todo", _fail)

    out = await taskflow_create.coroutine(
        session_id=SESSION, flow_id="flow-unbound", description="unbound probe"
    )

    assert out.startswith("Error:")
    assert "todo could not be bound" in out
    flow = await store_sqlite.get_flow("flow-unbound", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.FAILED.value
    assert flow["terminal_reason"] == "todo_bind_failed"


@pytest.mark.asyncio
async def test_flow_level_todo_cannot_complete_before_the_flow_ends(isolated_db: Path):
    out = await taskflow_create.coroutine(
        session_id=SESSION, flow_id="flow-root", description="root probe"
    )
    assert "TaskFlow created" in out
    todos = await todo_store.get_todos(SESSION)
    root = dict(todos[0], status="completed")

    with pytest.raises(TodoStoreError, match="Finish or cancel the flow"):
        await TodoService.update_todos(SESSION, [root])

    finished = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-root")
    assert "TaskFlow finished" in finished, finished
    await TodoService.update_todos(SESSION, [root])


@pytest.mark.asyncio
async def test_plan_auto_binds_its_goal(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    class _FakeLLM:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content=json.dumps([{"step_id": "s1", "task": "do the work"}]))

    monkeypatch.setattr("models.build_taskflow_llm", lambda **_kwargs: _FakeLLM())

    out = await taskflow_plan.coroutine(
        session_id=SESSION, flow_id="flow-plan", goal="write the report"
    )

    assert "TaskFlow planned" in out, out
    todos = await todo_store.get_todos(SESSION)
    assert todos[0]["flow_id"] == "flow-plan"
    assert todos[0]["content"] == "write the report"


@pytest.mark.asyncio
async def test_finish_nudge_budget_then_failure_budget(isolated_db: Path):
    await _create(
        "flow-nudge",
        steps=[
            {
                "step_id": "s1",
                "task": "running work",
                "status": "dispatched",
                "child_session_key": "agent:main:subagent:still-running",
            }
        ],
    )

    first = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-nudge")
    second = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-nudge")
    third = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-nudge")
    fourth = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-nudge")

    assert "nudge 1/2" in first, first
    assert "nudge 2/2" in second, second
    assert "finish rejection 1/2" in third, third
    assert "auto-failed" in fourth, fourth
    flow = await store_sqlite.get_flow("flow-nudge", SESSION)
    assert flow is not None
    assert flow["terminal_reason"] == "finish_gates_exhausted"
    assert flow["state"]["detached_child_session_keys"] == ["agent:main:subagent:still-running"]


@pytest.mark.asyncio
async def test_finish_failure_budget_auto_fails_a_loop(isolated_db: Path):
    await _create(
        "flow-gate-loop",
        steps=[{"step_id": "s1", "task": "blocked work", "status": "blocked"}],
    )

    first = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-gate-loop")
    second = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-gate-loop")

    assert first.startswith("Error:")
    assert "auto-failed" in second
    flow = await store_sqlite.get_flow("flow-gate-loop", SESSION)
    assert flow is not None
    assert flow["status"] == TaskFlowStatus.FAILED.value
    assert flow["terminal_reason"] == "finish_gates_exhausted"


@pytest.mark.asyncio
async def test_protocol_violation_budget_auto_fails(isolated_db: Path):
    await _create(
        "flow-protocol",
        steps=[{"step_id": "s1", "task": "ready work", "status": "ready"}],
    )

    outputs = [
        await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-protocol")
        for _ in range(3)
    ]

    assert outputs[-1].startswith("TaskFlow auto-failed"), outputs
    flow = await store_sqlite.get_flow("flow-protocol", SESSION)
    assert flow is not None
    assert flow["terminal_reason"] == "protocol_violation_exhausted"


@pytest.mark.asyncio
async def test_prune_keeps_active_rows_and_caps_terminal_rows(isolated_db: Path):
    for index in range(205):
        flow = await _create(f"flow-prune-{index}")
        if index < 200:
            await store_sqlite.update_flow(
                flow["flow_id"],
                flow["expected_revision"],
                session_id=SESSION,
                status=TaskFlowStatus.DONE.value,
                terminal_reason="completed",
            )

    deleted = await store_sqlite.prune_session_flows(SESSION)

    assert deleted == 5
    assert await store_sqlite.count_total_flows_for_session(SESSION) == 200
    active = store_sqlite.get_active_flows_sync(SESSION)
    assert len(active) == 5


@pytest.mark.asyncio
async def test_replace_all_is_one_commit(monkeypatch: pytest.MonkeyPatch):
    """The full-replace DELETE and INSERTs commit together (the GC race guard)."""
    recorded: list[str] = []

    class _FakeDB:
        async def execute(self, sql, _params):
            recorded.append("delete" if sql.lstrip().upper().startswith("DELETE") else "other")

        async def executemany(self, _sql, _rows):
            recorded.append("insert-many")

        async def commit(self):
            recorded.append("commit")

    class _Ctx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *_exc):
            return False

    async def _noop():
        return None

    monkeypatch.setattr(todo_store, "ensure_db", _noop)
    monkeypatch.setattr(todo_store, "_connect", lambda: _Ctx())

    await todo_store.replace_all(SESSION, [{"content": "atomic"}])

    assert recorded[0] == "delete"
    assert "insert-many" in recorded
    assert recorded[-1] == "commit"
    assert recorded.count("commit") == 1


@pytest.mark.asyncio
async def test_prune_zero_disables(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    await _create("flow-keep")
    monkeypatch.setitem(TASKFLOW_INFRA, "max_total_flows_per_session", 0)

    assert await store_sqlite.prune_session_flows(SESSION) == 0


# ---------------------------------------------------------------------------
# end-to-end: an orphan is actually collected, a rooted flow survives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_gc_end_to_end(isolated_db: Path):
    # 1. Entry defense creates the root automatically.
    created = await taskflow_create.coroutine(
        session_id=SESSION, flow_id="flow-e2e", description="e2e probe"
    )
    assert "TaskFlow created" in created

    # 2. Remove the root (simulating a full-replacement todowrite that dropped
    #    it), age the flow, and let tri-color collect it.
    await todo_store.replace_all(SESSION, [])
    _age(isolated_db, "flow-e2e", 600)
    swept = await sweeper._tri_color_sweep(now=_now(), grace_seconds=0)
    assert swept["swept"] == 1
    flow = await store_sqlite.get_flow("flow-e2e", SESSION)
    assert flow is not None and flow["status"] == TaskFlowStatus.FAILED.value

    # 3. A rooted flow in a dead session is collected by liveness, and its
    #    zombie todo is cancelled in the same pass.
    await _create("flow-e2e-live")
    await _todo("flow-e2e-live")
    _age(isolated_db, "flow-e2e-live", 13 * 3600)
    live = await sweeper._liveness_sweep(
        now=_now(), active_session_ids=set(), stale_action_hours=12
    )
    assert live["session_dead"] == 1
    assert live["todo_cancelled"] == 1
    todos = await todo_store.get_todos(SESSION)
    assert todos[0]["status"] == "cancelled"
