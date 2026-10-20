"""Session purge orchestration — every store a cleared session touches.

Lives in the service layer, not in ``server/DAO``: the purge spans the context
engine, the checkpointer, the planning stores, the plan-knowledge directories
and the runtime state registers, so a DAO (whose contract is pure data access
to its own store) was the wrong owner.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from loguru import logger
from pub.func.validator import is_safe_session_id

from config import SESSIONS_DIR
from runtime import clear_all_register_sessions
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history


def _session_folder(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / session_id).as_posix()


async def purge_session(session_id: str) -> None:
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
    except Exception:  # non-critical: must not block session cleanup
        logger.debug(
            "session continuity save failed on session end (continuing): session_id={}",
            session_id,
            exc_info=True,
        )

    # (1) Context engine mes_memory store — messages for this session. The bulk
    # DELETE is blocking SQLite, so it runs on a worker thread (audit-hygiene:
    # no store I/O on the event loop).
    from context_engine import delete_messages_by_session

    deleted = await asyncio.to_thread(delete_messages_by_session, session_id=session_id)
    logger.debug(f"Cleared {deleted} mes_memory message row(s) for session_id={session_id}")

    # (2) Session-scoped planning stores — todos + task flows.
    #
    # They live in two separate SQLite files, so a purge cannot be atomic.
    # Record durable intent on the flow rows first, then perform each delete
    # independently and clear its bit only on success. A failure leaves the bit
    # set for the sweeper's replay (agent/tools/subagent/registry/sweeper.py).
    from agent.tools.taskflow.config import (
        PURGE_BITS_ALL,
        PURGE_BIT_DANGLING_TODO_PTR,
        PURGE_BIT_FLOWS_NOT_DELETED,
        PURGE_BIT_TODOS_NOT_CLEARED,
    )
    from agent.tools.taskflow.registry import store_sqlite as taskflow_store
    from agent.tools.todolist.registry import store_sqlite as todo_store

    try:
        await taskflow_store.mark_purge_pending(session_id, mask=PURGE_BITS_ALL)
    except Exception as e:
        logger.warning(f"Could not record purge intent for session_id={session_id}: {e}")

    cleared_todos = 0
    try:
        cleared_todos = await todo_store.delete_todos_by_session(session_id)
        await taskflow_store.clear_purge_bit(session_id, PURGE_BIT_TODOS_NOT_CLEARED)
    except Exception as e:
        logger.warning(f"Failed to clear todos for session_id={session_id}: {e}")

    cleared_flows = 0
    try:
        flow_ids = await taskflow_store.get_flow_ids_by_session(session_id)
        # A todo linked to a purged flow would be permanently blocked by the
        # completion barrier; clear those pointers before deleting the flows.
        for flow_id in flow_ids:
            linked = await todo_store.get_todos_by_flow(session_id, flow_id)
            for todo in linked:
                await todo_store.clear_flow_link(session_id, int(todo["position"]))
        cleared_flows = await taskflow_store.delete_flows_by_session(session_id)
        await taskflow_store.clear_purge_bit(
            session_id, PURGE_BIT_FLOWS_NOT_DELETED | PURGE_BIT_DANGLING_TODO_PTR
        )
    except Exception as e:
        logger.warning(f"Failed to clear taskflows for session_id={session_id}: {e}")

    logger.debug(
        f"Cleared {cleared_todos} todo row(s) and {cleared_flows} taskflow row(s) "
        f"for session_id={session_id}"
    )

    # (3) sqlite checkpointer — checkpoints + writes for this thread.
    await delete_thread_history(session_id=session_id)

    # (3.5) Background terminal tasks: the processes are children of THIS
    # process, so a deleted session's live commands must be terminated before
    # the output files go away with the folder (best-effort — a dead pid is the
    # common case, and the watcher already recorded that exit).
    try:
        from agent.tools.terminal_bg_registry import background_registry

        killed = background_registry.kill_session_tasks(session_id)
        if killed:
            logger.debug(f"Terminated {killed} background terminal task(s) for {session_id}")
    except Exception as e:
        logger.warning(f"Failed to terminate background terminal tasks for {session_id}: {e}")

    # (3.6) The session's sub-agent spawn budget (in-memory, keyed by the
    # prefixed main-session key). Dropped with the session so a re-created id
    # starts from a clean budget.
    try:
        from agent.tools.subagent.registry.session_keys import denormalize_session_key
        from agent.tools.subagent.registry.spawn_budget import reset_spawn_count

        reset_spawn_count(denormalize_session_key(session_id))
    except Exception as e:
        logger.warning(f"Failed to reset the subagent spawn budget for {session_id}: {e}")

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
