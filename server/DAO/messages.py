import shutil
from pathlib import Path

from loguru import logger
from pub.func.validator import is_safe_session_id

from config import SESSIONS_DIR
from runtime import clear_all_register_sessions
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history


def _session_folder(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / session_id).as_posix()


async def clear_session(session_id: str) -> None:
    """Purge every trace of a session across all stores.

    Deletes, in order:
      0. Save the session end state for cross-session continuity —
         BEFORE any deletion, so the tail messages still exist.
      1. The session's rows from the context engine SQLite store
         (``mes_memory.db`` messages table).
      2. The session's todo rows (``todos.db``) and task-flow rows
         (``taskflow_registry.db``) — best-effort: an auxiliary-store failure
         is logged and never blocks the rest of the purge. Pre-isolation
         task-flow rows (``session_id = ''``) are never matched.
      3. The session's records from the sqlite checkpointer
         (``src/checkpoints/sqlite.db`` — checkpoints + writes).
      4. The session's folder under the ``sessions`` directory.
      5. The session's private plan-knowledge directories (session-scoped
         knowledge is keyed by plan identity, not by session; plans shared
         with another session through boulder ``session_ids`` are retained).
      6. The in-memory session state via ``clear_all_register_sessions``.
      7. The session's variables from the ``state_register_db`` SQLite store.

    Raises:
        ValueError: If ``session_id`` is not a single safe path segment
            (the purge reaches ``shutil.rmtree``, so a traversal
            id must never get past this boundary).
    """
    if not is_safe_session_id(session_id):
        raise ValueError(f"invalid session_id: {session_id!r}")
    # (0) Session continuity: persist the tail state before deletion.
    try:
        from context_engine.session_continuity import auto_save_on_session_end

        await auto_save_on_session_end(session_id)
    except Exception:  # noqa: S110 - non-critical: must not block session cleanup
        pass

    # (1) Context engine mes_memory store — messages for this session.
    from context_engine import delete_messages_by_session

    deleted = delete_messages_by_session(session_id=session_id)
    logger.debug(f"Cleared {deleted} mes_memory message row(s) for session_id={session_id}")

    # (2) Session-scoped planning stores — todos + task flows.
    try:
        from agent.tools.taskflow.registry import store_sqlite as taskflow_store
        from agent.tools.todolist.registry import store_sqlite as todo_store

        cleared_todos = await todo_store.delete_todos_by_session(session_id)
        cleared_flows = await taskflow_store.delete_flows_by_session(session_id)
        logger.debug(
            f"Cleared {cleared_todos} todo row(s) and {cleared_flows} taskflow row(s) "
            f"for session_id={session_id}"
        )
    except Exception as e:
        logger.warning(f"Failed to clear planning stores for session_id={session_id}: {e}")

    # (3) sqlite checkpointer — checkpoints + writes for this thread.
    await delete_thread_history(session_id=session_id)

    # (4) Session folder under the sessions directory.
    path = Path(_session_folder(session_id))
    if path.exists() and path.is_dir():
        shutil.rmtree(path)

    # (5) Plan knowledge produced for this session's own plans (best-effort).
    try:
        from agent.tools.todolist.knowledge import clear_session_plan_knowledge

        purged = clear_session_plan_knowledge(session_id)
        logger.debug(f"Cleared {purged} plan-knowledge director(ies) for session_id={session_id}")
    except Exception as e:
        logger.warning(f"Failed to clear plan knowledge for session_id={session_id}: {e}")

    # (6) In-memory register sessions (e.g. StateRegisterMeM) and state_register_db — delete every keyed variable for this session.
    clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)
