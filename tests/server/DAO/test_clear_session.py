"""Clear-session tests for the planning stores.

``server.DAO.messages.clear_session`` purges "every trace of a session": the
message store, the checkpointer, the session folder — and, since session
isolation, the session's todo rows and task-flow rows. Other sessions' rows must
survive untouched.
"""

import asyncio
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]


def _isolate_store(monkeypatch: pytest.MonkeyPatch, module, db_name: str, tmp_path: Path) -> None:
    """Point a store module at a per-test tmp SQLite file, resetting init state."""
    monkeypatch.setattr(module, "_DB_DIR", tmp_path)
    monkeypatch.setattr(module, "_DB_PATH", tmp_path / db_name)
    monkeypatch.setattr(module, "_initialized", False)
    monkeypatch.setattr(module, "_init_loop", None)
    monkeypatch.setattr(module, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(module, "_sync_tables_ready", False)


@pytest.mark.asyncio
async def test_clear_session_purges_only_that_sessions_planning_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent.tools.taskflow.registry import store_sqlite as flow_store
    from agent.tools.todolist.registry import store_sqlite as todo_store
    from server.DAO import messages as dao

    _isolate_store(monkeypatch, todo_store, "todos.db", tmp_path)
    _isolate_store(monkeypatch, flow_store, "taskflow_registry.db", tmp_path)

    async def _noop(session_id: str) -> None:
        return None

    monkeypatch.setattr("context_engine.session_continuity.auto_save_on_session_end", _noop)
    monkeypatch.setattr("context_engine.delete_messages_by_session", lambda session_id: 0)
    monkeypatch.setattr(dao, "delete_thread_history", _noop)
    monkeypatch.setattr(dao, "clear_all_register_sessions", lambda *a, **k: None)
    monkeypatch.setattr(dao, "SESSIONS_DIR", tmp_path / "sessions")

    await todo_store.replace_all("sess-1", [{"content": "a", "status": "pending"}])
    await todo_store.replace_all("sess-2", [{"content": "b", "status": "pending"}])
    state = {"description": "x", "steps": [], "results": []}
    await flow_store.create_flow("flow-1", state, session_id="sess-1")
    await flow_store.create_flow("flow-2", state, session_id="sess-2")

    await dao.clear_session("sess-1")

    assert await todo_store.get_todos("sess-1") == []
    assert [todo["content"] for todo in await todo_store.get_todos("sess-2")] == ["b"]
    assert await flow_store.get_flow("flow-1", "sess-1") is None
    assert await flow_store.get_flow("flow-2", "sess-2") is not None
