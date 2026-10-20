"""Purge-intent replay across the two planning stores (plan §1.10)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.tools.subagent.registry import sweeper
from agent.tools.taskflow.config import PURGE_BITS_ALL, PURGE_BIT_FLOWS_NOT_DELETED
from agent.tools.taskflow.registry import store_sqlite as taskflow_store
from agent.tools.todolist.registry import store_sqlite as todo_store

pytestmark = [pytest.mark.unit]

SESSION = "sess-purge"


@pytest.fixture()
def todo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "todos.db"
    monkeypatch.setattr(todo_store, "_DB_DIR", tmp_path)
    monkeypatch.setattr(todo_store, "_DB_PATH", path)
    monkeypatch.setattr(todo_store, "_initialized", False)
    monkeypatch.setattr(todo_store, "_init_loop", None)
    monkeypatch.setattr(todo_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(todo_store, "_sync_tables_ready", False)
    return path


async def _setup(*, todo_linked: bool = True) -> None:
    await taskflow_store.create_flow("flow-1", {"steps": [], "results": []}, session_id=SESSION)
    if todo_linked:
        await todo_store.replace_all(
            SESSION,
            [{"content": "linked", "position": 0, "flow_id": "flow-1", "step_id": "step-1"}],
        )


@pytest.mark.asyncio
async def test_the_dangling_pointer_replay_clears_the_todo_link(isolated_db: Path, todo_db: Path):
    from agent.tools.taskflow.config import PURGE_BIT_DANGLING_TODO_PTR

    await _setup()
    await taskflow_store.mark_purge_pending(SESSION, mask=PURGE_BIT_DANGLING_TODO_PTR)

    assert await sweeper._reconcile_purge_pending() == 1

    todos = await todo_store.get_todos(SESSION)
    assert todos[0]["flow_id"] is None and todos[0]["step_id"] is None
    flow = await taskflow_store.get_flow("flow-1", SESSION)
    assert flow is not None  # this bit does not delete the flow
    assert flow["purge_pending"] == 0


@pytest.mark.asyncio
async def test_the_full_purge_replay_deletes_both_stores(isolated_db: Path, todo_db: Path):
    await _setup()
    await taskflow_store.mark_purge_pending(SESSION, mask=PURGE_BITS_ALL)

    await sweeper._reconcile_purge_pending()

    assert await taskflow_store.get_flow("flow-1", SESSION) is None
    assert await todo_store.get_todos(SESSION) == []


@pytest.mark.asyncio
async def test_a_failed_flow_delete_keeps_the_durable_bit(
    isolated_db: Path, todo_db: Path, monkeypatch: pytest.MonkeyPatch
):
    await _setup()
    await taskflow_store.mark_purge_pending(SESSION, mask=PURGE_BITS_ALL)

    async def _boom(_session_id: str) -> int:
        raise RuntimeError("taskflow db locked")

    monkeypatch.setattr(taskflow_store, "delete_flows_by_session", _boom)

    await sweeper._reconcile_purge_pending()

    flow = await taskflow_store.get_flow("flow-1", SESSION)
    assert flow is not None
    # Todos were cleared (bit 1 gone); the failed flow delete keeps bit 2.
    assert await todo_store.get_todos(SESSION) == []
    assert flow["purge_pending"] & PURGE_BIT_FLOWS_NOT_DELETED
