"""Todo↔TaskFlow reference integrity.

A full ``todowrite`` replacement that drops a flow's GC-root todo compensates
immediately (cancel/refuse/sweep_only, fail-open); a session-scoped sweep follows
as the backstop and catches aged orphans. Create/plan report a similar active
flow unless ``ack_dup=True``. A terminal flow notifies its GC-root todo
immediately (completed on finish, cancelled on fail/cancel), idempotently and
fail-open.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from agent.tools.subagent.registry import sweeper
from agent.tools.taskflow.config import TABLE_NAME
from agent.tools.taskflow.registry import store_sqlite as flow_store
from agent.tools.taskflow.tools._duplicates import find_duplicate_flow, token_overlap
from agent.tools.taskflow.tools.taskflow_cancel import taskflow_cancel
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_fail import taskflow_fail
from agent.tools.taskflow.tools.taskflow_finish import taskflow_finish
from agent.tools.todolist import service as todo_service
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.todolist.service import TodoReferenceError, TodoService
from config.features import TODOLIST_INFRA

pytestmark = [pytest.mark.unit]

SESSION = "sess-integrity"
OTHER = "sess-integrity-other"


async def _create_flow(
    flow_id: str,
    *,
    session_id: str = SESSION,
    description: str | None = None,
    ack_dup: bool = True,
) -> str:
    return await taskflow_create.coroutine(
        flow_id=flow_id,
        description=description or f"work for {flow_id}",
        session_id=session_id,
        ack_dup=ack_dup,
    )


async def _replace(session_id: str, todos: list[dict]) -> list[dict]:
    return await TodoService.update_todos(session_id, todos)


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


def _consume(session_id: str = SESSION) -> list[str]:
    return TodoService.consume_reference_warning(session_id)


# ---------------------------------------------------------------------------
# A dropped GC root is compensated immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_dropping_root_cancels_flow_and_warns(isolated_db: Path):
    await _create_flow("flow-p1-cancel")

    await _replace(SESSION, [{"content": "a completely different plan"}])

    flow = await flow_store.get_flow("flow-p1-cancel", SESSION)
    assert flow is not None and flow["status"] == "failed"
    assert flow["state"]["failure_reason"] == "todo_dropped_in_full_replace"
    assert _consume() == ["flow-p1-cancel"]
    assert _consume() == []  # warning is consumed once


@pytest.mark.asyncio
async def test_replace_keeping_the_root_does_not_compensate(isolated_db: Path):
    await _create_flow("flow-p1-keep")
    fallback = (await todo_store.get_todos(SESSION))[0]

    await _replace(
        SESSION,
        [{"content": fallback["content"], "flow_id": "flow-p1-keep", "status": "pending"}],
    )

    flow = await flow_store.get_flow("flow-p1-keep", SESSION)
    assert flow is not None and flow["status"] == "running"
    assert _consume() == []


@pytest.mark.asyncio
async def test_refuse_mode_rejects_the_replacement(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(TODOLIST_INFRA, "orphan_flow_on_drop", "refuse")
    await _create_flow("flow-p1-refuse")
    before = await todo_store.get_todos(SESSION)

    with pytest.raises(TodoReferenceError, match="flow-p1-refuse"):
        await _replace(SESSION, [{"content": "replacement"}])

    flow = await flow_store.get_flow("flow-p1-refuse", SESSION)
    assert flow is not None and flow["status"] == "running"
    assert await todo_store.get_todos(SESSION) == before
    assert _consume() == []


@pytest.mark.asyncio
async def test_sweep_only_mode_skips_immediate_compensation(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(TODOLIST_INFRA, "orphan_flow_on_drop", "sweep_only")
    await _create_flow("flow-p1-sweep-only")

    await _replace(SESSION, [{"content": "replacement"}])

    # The configured grace window protects a just-created flow from the
    # post-replace sweep; only the targeted compensation would have collected it here.
    flow = await flow_store.get_flow("flow-p1-sweep-only", SESSION)
    assert flow is not None and flow["status"] == "running"
    assert _consume() == []


@pytest.mark.asyncio
async def test_terminal_flow_is_skipped_by_compensation(isolated_db: Path):
    await _create_flow("flow-p1-terminal")
    assert "status=failed" in await taskflow_fail.coroutine(
        session_id=SESSION, flow_id="flow-p1-terminal", reason="already broken"
    )
    assert await flow_store.get_flow("flow-p1-terminal", SESSION) is not None

    await _replace(SESSION, [{"content": "replacement"}])

    assert _consume() == []
    flow = await flow_store.get_flow("flow-p1-terminal", SESSION)
    assert flow is not None and flow["status"] == "failed"


@pytest.mark.asyncio
async def test_compensation_failure_does_not_block_the_replacement(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _create_flow("flow-p1-boom")

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("flow store unavailable")

    monkeypatch.setattr(flow_store, "get_flow", _boom)

    result = await _replace(SESSION, [{"content": "replacement"}])

    assert [todo["content"] for todo in result] == ["replacement"]
    assert _consume() == []


@pytest.mark.asyncio
async def test_missing_flow_is_skipped(isolated_db: Path):
    await todo_store.replace_all(
        SESSION, [{"content": "ghost root", "status": "pending", "flow_id": "flow-ghost"}]
    )

    result = await _replace(SESSION, [{"content": "replacement"}])

    assert [todo["content"] for todo in result] == ["replacement"]
    assert _consume() == []


# ---------------------------------------------------------------------------
# Session-scoped sweep after a replacement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_triggers_one_session_sweep(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []

    async def _spy(session_id: str) -> None:
        calls.append(session_id)

    monkeypatch.setattr(todo_service, "_trigger_session_sweep", _spy)

    await _replace(SESSION, [{"content": "replacement"}])

    assert calls == [SESSION]


@pytest.mark.asyncio
async def test_post_replace_sweep_collects_an_aged_orphan(isolated_db: Path):
    await flow_store.create_flow(
        "flow-p3-old", {"description": "aged", "steps": [], "results": []}, session_id=SESSION
    )
    _age(isolated_db, "flow-p3-old", 10_000)

    await _replace(SESSION, [{"content": "replacement"}])

    flow = await flow_store.get_flow("flow-p3-old", SESSION)
    assert flow is not None and flow["status"] == "failed"
    assert flow["state"]["failure_reason"] == "orphan_unreferenced"


@pytest.mark.asyncio
async def test_post_replace_sweep_failure_is_fail_open(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    async def _boom(**_kwargs: object) -> dict[str, int]:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(sweeper, "_unreferenced_flow_sweep", _boom)

    result = await _replace(SESSION, [{"content": "replacement"}])

    assert [todo["content"] for todo in result] == ["replacement"]


@pytest.mark.asyncio
async def test_trigger_sweep_flag_off_skips_the_pass(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(TODOLIST_INFRA, "trigger_sweep_after_replace", False)
    calls: list[str] = []

    async def _spy(session_id: str) -> None:
        calls.append(session_id)

    monkeypatch.setattr(todo_service, "_trigger_session_sweep", _spy)

    await _replace(SESSION, [{"content": "replacement"}])

    assert calls == []


# ---------------------------------------------------------------------------
# Duplicate-flow warning on create/plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_reports_a_similar_active_flow(isolated_db: Path):
    first = await _create_flow("flow-p2-a", description="implement the login flow for users")
    assert "TaskFlow created" in first or "flow_id=flow-p2-a" in first

    out = await _create_flow(
        "flow-p2-b", description="implement the login flow for users", ack_dup=False
    )

    assert "Possible duplicate" in out and "flow-p2-a" in out
    assert await flow_store.get_flow("flow-p2-b", SESSION) is None


@pytest.mark.asyncio
async def test_create_ack_dup_skips_the_check(isolated_db: Path):
    await _create_flow("flow-p2-c", description="implement the login flow")

    out = await _create_flow("flow-p2-d", description="implement the login flow", ack_dup=True)

    assert "Possible duplicate" not in out
    assert await flow_store.get_flow("flow-p2-d", SESSION) is not None


@pytest.mark.asyncio
async def test_terminal_flow_is_not_a_duplicate(isolated_db: Path):
    await _create_flow("flow-p2-done", description="implement the login flow")
    assert "TaskFlow finished" in await taskflow_finish.coroutine(
        session_id=SESSION, flow_id="flow-p2-done"
    )

    out = await _create_flow("flow-p2-new", description="implement the login flow", ack_dup=False)

    assert "Possible duplicate" not in out
    assert await flow_store.get_flow("flow-p2-new", SESSION) is not None


@pytest.mark.asyncio
async def test_duplicate_check_is_session_scoped(isolated_db: Path):
    await _create_flow("flow-p2-other", session_id=OTHER, description="implement the login flow")

    out = await _create_flow(
        "flow-p2-mine", session_id=SESSION, description="implement the login flow", ack_dup=False
    )

    assert "Possible duplicate" not in out
    assert await flow_store.get_flow("flow-p2-mine", SESSION) is not None


@pytest.mark.asyncio
async def test_token_overlap_branch_detects_a_reordered_goal(isolated_db: Path):
    await flow_store.create_flow(
        "flow-p2-overlap",
        {"description": "alpha beta gamma delta", "steps": [], "results": []},
        session_id=SESSION,
    )

    out = await find_duplicate_flow(
        SESSION, "delta gamma beta alpha epsilon", "delta gamma beta alpha epsilon"
    )

    assert out is not None and out["flow_id"] == "flow-p2-overlap"
    assert token_overlap("alpha beta gamma delta", "delta gamma beta alpha epsilon") > 0.7


def test_normalized_substring_is_detected():
    from agent.tools.taskflow.tools._duplicates import normalize_duplicate_text

    assert normalize_duplicate_text("  Implement   LOGIN  ") == "implement login"


# ---------------------------------------------------------------------------
# A terminal flow notifies its GC-root todo immediately
# ---------------------------------------------------------------------------


async def _root_status(flow_id: str) -> list[str]:
    todos = await todo_store.get_todos(SESSION)
    return [todo["status"] for todo in todos if todo["flow_id"] == flow_id]


@pytest.mark.asyncio
async def test_finish_marks_the_root_todo_completed(isolated_db: Path):
    await _create_flow("flow-p4-done")
    assert await _root_status("flow-p4-done") == ["pending"]

    out = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-p4-done")

    assert "TaskFlow finished" in out
    assert await _root_status("flow-p4-done") == ["completed"]


@pytest.mark.asyncio
async def test_fail_marks_the_root_todo_cancelled(isolated_db: Path):
    await _create_flow("flow-p4-fail")

    out = await taskflow_fail.coroutine(session_id=SESSION, flow_id="flow-p4-fail", reason="bad")

    assert "status=failed" in out
    assert await _root_status("flow-p4-fail") == ["cancelled"]


@pytest.mark.asyncio
async def test_cancel_marks_the_root_todo_cancelled(isolated_db: Path):
    await _create_flow("flow-p4-cancel")

    out = await taskflow_cancel.coroutine(
        session_id=SESSION, flow_id="flow-p4-cancel", reason="stop"
    )

    assert "status=cancelled" in out
    assert await _root_status("flow-p4-cancel") == ["cancelled"]


@pytest.mark.asyncio
async def test_terminal_todo_notification_is_idempotent(isolated_db: Path):
    await _create_flow("flow-p4-idem")
    await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-p4-idem")

    assert await TodoService.complete_todos_for_flows(SESSION, ["flow-p4-idem"]) == 0
    assert await TodoService.cancel_todos_for_flows(SESSION, ["flow-p4-idem"]) == 0
    assert await _root_status("flow-p4-idem") == ["completed"]


@pytest.mark.asyncio
async def test_finish_survives_a_notification_failure(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _create_flow("flow-p4-finish-boom")

    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("todo store unavailable")

    monkeypatch.setattr(TodoService, "complete_todos_for_flows", _boom)

    out = await taskflow_finish.coroutine(session_id=SESSION, flow_id="flow-p4-finish-boom")

    assert "TaskFlow finished" in out
    flow = await flow_store.get_flow("flow-p4-finish-boom", SESSION)
    assert flow is not None and flow["status"] == "done"


@pytest.mark.asyncio
async def test_fail_survives_a_notification_failure(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _create_flow("flow-p4-fail-boom")

    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("todo store unavailable")

    monkeypatch.setattr(TodoService, "cancel_todos_for_flows", _boom)

    out = await taskflow_fail.coroutine(
        session_id=SESSION, flow_id="flow-p4-fail-boom", reason="bad"
    )

    assert "status=failed" in out


@pytest.mark.asyncio
async def test_replace_dropping_all_roots_still_returns_the_new_list(isolated_db: Path):
    await _create_flow("flow-p4-r1")
    await _create_flow("flow-p4-r2")

    result = await _replace(SESSION, [{"content": "one"}] * 1)

    assert [todo["content"] for todo in result] == ["one"]
    assert sorted(_consume()) == ["flow-p4-r1", "flow-p4-r2"]
    for flow_id in ("flow-p4-r1", "flow-p4-r2"):
        flow = await flow_store.get_flow(flow_id, SESSION)
        assert flow is not None and flow["status"] == "failed"


@pytest.mark.asyncio
async def test_dropping_a_root_kills_the_flow_live_children(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace

    from agent.tools.subagent.types.registry import ExecutionStatus
    import agent.tools.subagent.control.kill as kill_mod
    import agent.tools.subagent.registry.queries as queries

    await flow_store.create_flow(
        "flow-kill",
        {
            "description": "kill probe",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "do it",
                    "depends_on": [],
                    "status": "dispatched",
                    "child_session_key": "agent:main:subagent:child",
                }
            ],
            "results": [],
        },
        session_id=SESSION,
    )
    await todo_store.replace_all(
        SESSION, [{"content": "root", "status": "pending", "flow_id": "flow-kill"}]
    )
    run = SimpleNamespace(
        run_id="run-1",
        controller_session_key="agent:main:session:sess-integrity",
        execution=SimpleNamespace(status=ExecutionStatus.RUNNING),
    )
    killed: list[tuple[str, str]] = []

    monkeypatch.setattr(queries, "get_run_by_child_session_key", lambda _key: run)

    async def _kill(run_id: str, reason: str, **_kwargs: object) -> list:
        killed.append((run_id, reason))
        return []

    monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _kill)

    await _replace(SESSION, [{"content": "replacement"}])

    assert killed and killed[0][0] == "run-1"
    assert "flow root dropped" in killed[0][1]


@pytest.mark.asyncio
async def test_child_cleanup_skips_a_terminal_run(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace

    from agent.tools.subagent.types.registry import ExecutionStatus
    import agent.tools.subagent.control.kill as kill_mod
    import agent.tools.subagent.registry.queries as queries

    await flow_store.create_flow(
        "flow-kill-done",
        {
            "description": "kill probe",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "done",
                    "depends_on": [],
                    "status": "dispatched",
                    "child_session_key": "agent:main:subagent:child",
                }
            ],
            "results": [],
        },
        session_id=SESSION,
    )
    await todo_store.replace_all(
        SESSION, [{"content": "root", "status": "pending", "flow_id": "flow-kill-done"}]
    )
    monkeypatch.setattr(
        queries,
        "get_run_by_child_session_key",
        lambda _key: SimpleNamespace(
            run_id="run-1",
            controller_session_key=None,
            execution=SimpleNamespace(status=ExecutionStatus.TERMINAL),
        ),
    )
    killed: list[str] = []

    async def _kill(run_id: str, **_kwargs: object) -> list:
        killed.append(run_id)
        return []

    monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _kill)

    await _replace(SESSION, [{"content": "replacement"}])

    assert killed == []


@pytest.mark.asyncio
async def test_child_cleanup_failure_does_not_block_the_replacement(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace

    from agent.tools.subagent.types.registry import ExecutionStatus
    import agent.tools.subagent.control.kill as kill_mod
    import agent.tools.subagent.registry.queries as queries

    await flow_store.create_flow(
        "flow-kill-boom",
        {
            "description": "kill probe",
            "steps": [
                {
                    "step_id": "s1",
                    "task": "do it",
                    "depends_on": [],
                    "status": "dispatched",
                    "child_session_key": "agent:main:subagent:child",
                }
            ],
            "results": [],
        },
        session_id=SESSION,
    )
    await todo_store.replace_all(
        SESSION, [{"content": "root", "status": "pending", "flow_id": "flow-kill-boom"}]
    )
    monkeypatch.setattr(
        queries,
        "get_run_by_child_session_key",
        lambda _key: SimpleNamespace(
            run_id="run-1",
            controller_session_key=None,
            execution=SimpleNamespace(status=ExecutionStatus.RUNNING),
        ),
    )

    async def _boom(*_args: object, **_kwargs: object) -> list:
        raise RuntimeError("registry down")

    monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _boom)

    result = await _replace(SESSION, [{"content": "replacement"}])

    assert [todo["content"] for todo in result] == ["replacement"]
