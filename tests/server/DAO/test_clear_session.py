"""Clear-session tests for the planning stores.

``server.DAO.messages.clear_session`` purges "every trace of a session": the
message store, the checkpointer, the session folder, the session's todo /
task-flow rows (since session isolation) and the session's private
plan-knowledge directories (since plan identity). Other sessions' rows and
shared plan knowledge must survive untouched.
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


def _isolate_dao(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neutralize every non-planning store the purge touches."""

    async def _noop(session_id: str) -> None:
        return None

    from server.DAO import messages as dao

    monkeypatch.setattr("context_engine.session_continuity.auto_save_on_session_end", _noop)
    monkeypatch.setattr("context_engine.delete_messages_by_session", lambda session_id: 0)
    monkeypatch.setattr(dao, "delete_thread_history", _noop)
    monkeypatch.setattr(dao, "clear_all_register_sessions", lambda *a, **k: None)
    monkeypatch.setattr(dao, "SESSIONS_DIR", tmp_path / "sessions")


@pytest.mark.asyncio
async def test_clear_session_purges_only_that_sessions_planning_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent.tools.taskflow.registry import store_sqlite as flow_store
    from agent.tools.todolist.registry import store_sqlite as todo_store
    from server.DAO import messages as dao

    _isolate_store(monkeypatch, todo_store, "todos.db", tmp_path)
    _isolate_store(monkeypatch, flow_store, "taskflow_registry.db", tmp_path)
    _isolate_dao(monkeypatch, tmp_path)

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


@pytest.mark.asyncio
async def test_clear_session_purges_session_private_plan_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent.tools.todolist.knowledge import knowledge_store as store_mod
    from agent.tools.todolist.knowledge import ownership as knowledge_ownership
    from agent.tools.todolist.knowledge.identity import resolve_plan_identity
    from server.DAO import messages as dao

    _isolate_dao(monkeypatch, tmp_path)

    class _FakeStateDB:
        def __init__(self) -> None:
            self._states: dict[str, dict[str, object]] = {}

        def get_state(self, session_id: str, key: str, default: object = None) -> object:
            return self._states.get(session_id, {}).get(key, default)

        def set_state(self, session_id: str, key: str, value: object) -> bool:
            self._states.setdefault(session_id, {})[key] = value
            return True

    state_db = _FakeStateDB()
    state_db.set_state("sess-1", "plan_ref", "workspace/sessions/sess-1/plans/x.md")
    state_db.set_state("sess-2", "plan_ref", "workspace/sessions/sess-2/plans/x.md")
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(store_mod, "_KNOWLEDGE_ROOT", knowledge_root)
    monkeypatch.setattr(knowledge_ownership, "state_register_db", state_db)
    monkeypatch.setattr(knowledge_ownership, "get_todos_sync", lambda session_id: [])
    monkeypatch.setattr(
        knowledge_ownership, "resolve_boulder_path", lambda: tmp_path / "no-boulder.json"
    )

    identity_1 = resolve_plan_identity("sess-1", "x")
    identity_2 = resolve_plan_identity("sess-2", "x")
    assert identity_1 is not None and identity_2 is not None
    await store_mod.KnowledgeStore.write(identity_1, layer="plan", data={"method": "one"})
    await store_mod.KnowledgeStore.write(identity_2, layer="plan", data={"method": "two"})

    await dao.clear_session("sess-1")

    assert (knowledge_root / identity_1.key).exists() is False
    assert (knowledge_root / identity_2.key / "plan-summary.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil_sid", ["../../workspace", "../workspace", "a/b", "a\\b", "..", ".", ""]
)
async def test_clear_session_rejects_unsafe_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evil_sid: str
):
    """A traversal session_id must never reach ``shutil.rmtree``."""
    from server.DAO import messages as dao

    _isolate_dao(monkeypatch, tmp_path)

    # A decoy the traversal would have destroyed if the guard were missing.
    victim = tmp_path / "workspace"
    victim.mkdir()

    with pytest.raises(ValueError, match="invalid session_id"):
        await dao.clear_session(evil_sid)

    assert victim.is_dir()
    assert list((tmp_path / "sessions").glob("*")) == []
