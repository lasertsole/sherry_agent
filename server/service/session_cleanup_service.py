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
    """Purge a session and its whole sub-agent descendant tree.

    Continuity is saved for the MAIN session first, then the descendant
    registry rows/checkpoints/session data are removed, then the main session's
    own stores are purged.
    """
    if not is_safe_session_id(session_id):
        raise ValueError(f"invalid session_id: {session_id!r}")
    try:
        from context_engine.session_continuity import auto_save_on_session_end

        await auto_save_on_session_end(session_id)
    except Exception:  # non-critical: must not block session cleanup
        logger.debug(
            "session continuity save failed on session end (continuing): session_id={}",
            session_id,
            exc_info=True,
        )
    try:
        await purge_subagent_subtree_for_session(session_id)
    except Exception as e:  # a broken child tree must not keep the main session
        logger.warning(f"Failed to purge the subagent subtree for {session_id}: {e}")
    # Side chats are ephemeral children: deleting their parent deletes them,
    # including their checkpoint/history/runtime state. Fail-open per child.
    try:
        from server.service.side_chat_service import purge_side_chat_children

        await purge_side_chat_children(session_id)
    except Exception as e:  # noqa: BLE001 - a broken child link must not keep the parent
        logger.warning(f"Failed to purge side-chat children for {session_id}: {e}")
    await purge_session_data(session_id)


async def purge_session_data(session_id: str) -> None:
    """Purge one session's own stores (no descendant traversal).

    Shared by the main-session purge and by each child's cascade purge. The
    caller owns continuity saving and any sub-agent tree work.

    Deletes, in order:
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

    # (1) Context engine mes_memory store — messages for this session. The bulk
    # DELETE is blocking SQLite, so it runs on a worker thread (audit-hygiene:
    # no store I/O on the event loop).
    from context_engine import delete_messages_by_session

    deleted = await asyncio.to_thread(delete_messages_by_session, session_id=session_id)
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

    # (7) Evidence ledger rows — one indexed DELETE per session (best-effort).
    try:
        from config.features import EVIDENCE_LEDGER

        if EVIDENCE_LEDGER["purge_on_session_delete"]:
            from agent.tools.pub_base.evidence_ledger import EvidenceLedger

            removed = await asyncio.to_thread(EvidenceLedger.purge_session, session_id)
            if removed:
                logger.debug(f"Deleted {removed} evidence row(s) for session_id={session_id}")
    except Exception as e:
        logger.warning(f"Failed to purge evidence for session_id={session_id}: {e}")


async def purge_subagent_subtree_for_session(
    main_session_id: str, *, reason: str = "session_purge"
) -> dict[str, int]:
    """Delete every descendant sub-agent run of one session, leaf stores included.

    Collects the whole requester tree BEFORE deleting registry rows (otherwise
    the BFS loses its edges), suppresses announce/delivery, kills live children,
    then purges each child's checkpoints, session folder, planning stores,
    state registers, attachments and registry row. Fail-open per child.
    """
    from agent.tools.subagent.control.kill import kill_subagent_run_admin
    from agent.tools.subagent.registry import (
        get_run,
        is_live_unended_run,
        list_descendant_runs,
        remove_run,
        update_run,
    )
    from agent.tools.subagent.registry.helpers import safe_remove_attachments_dir
    from agent.tools.subagent.registry.session_keys import denormalize_session_key

    root = denormalize_session_key(main_session_id)
    runs = list_descendant_runs(root)
    if not runs:
        return dict(descendants=0, checkpoints=0, registry_rows=0, attachments=0, errors=0)

    # Suppress delivery/announce before killing: a cancelled child must not
    # re-create a registry row or announce after this purge.
    for run in runs:
        try:
            update_run(
                run.run_id,
                suppress_announce_reason=reason,
                suppress_completion_delivery=True,
            )
        except Exception:
            logger.warning(
                "Failed to suppress subagent run {} during purge", run.run_id, exc_info=True
            )

    for run in runs:
        current = get_run(run.run_id) or run
        if is_live_unended_run(current):
            try:
                await kill_subagent_run_admin(current.run_id, reason=reason)
            except Exception:
                logger.warning(
                    "Failed to kill subagent run {} during purge", current.run_id, exc_info=True
                )

    deleted_checkpoints = 0
    deleted_rows = 0
    deleted_attachments = 0
    errors = 0
    for run in runs:
        try:
            await purge_session_data(run.child_session_key)
            await remove_run(run.run_id)
            deleted_rows += 1
            if run.attachments_dir:
                safe_remove_attachments_dir(run.attachments_dir, run.attachments_root_dir)
                deleted_attachments += 1
            # The checkpoint row count is not returned by delete_thread_history;
            # the call itself is the deletion. Count one logical thread per run.
            deleted_checkpoints += 1
        except Exception:
            errors += 1
            logger.warning("Failed to purge subagent run {}", run.run_id, exc_info=True)

    logger.info(
        "Purged subagent subtree of {}: descendants={}, registry_rows={}, "
        "checkpoint_threads={}, attachments={}, errors={}",
        main_session_id,
        len(runs),
        deleted_rows,
        deleted_checkpoints,
        deleted_attachments,
        errors,
    )
    return dict(
        descendants=len(runs),
        checkpoints=deleted_checkpoints,
        registry_rows=deleted_rows,
        attachments=deleted_attachments,
        errors=errors,
    )
