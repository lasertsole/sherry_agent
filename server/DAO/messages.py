import shutil
from pathlib import Path
from loguru import logger
from config import SESSIONS_DIR
from runtime import clear_all_register_sessions, state_register_db
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history

def _session_folder(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / session_id).as_posix()

async def clear_session(session_id: str) -> None:
    """Purge every trace of a session across all stores.

    Deletes, in order:
      1. The session's rows from the context engine SQLite store
         (``mes_memory.db`` messages table).
      2. The session's records from the sqlite checkpointer
         (``src/checkpoints/sqlite.db`` — checkpoints + writes).
      3. The session's folder under the ``sessions`` directory.
      4. The in-memory session state via ``clear_all_register_sessions``.
      5. The session's variables from the ``state_register_db`` SQLite store.
    """
    # (1) Context engine mes_memory store — messages for this session.
    from context_engine import delete_messages_by_session
    deleted = delete_messages_by_session(session_id=session_id)
    logger.debug(f"Cleared {deleted} mes_memory message row(s) for session_id={session_id}")

    # (2) sqlite checkpointer — checkpoints + writes for this thread.
    await delete_thread_history(session_id=session_id)

    # (3) Session folder under the sessions directory.
    path = Path(_session_folder(session_id))
    if path.exists() and path.is_dir():
        shutil.rmtree(path)

    # (4) In-memory register sessions (e.g. StateRegisterMeM).
    clear_all_register_sessions(session_id)

    # (5) state_register_db — delete every keyed variable for this session.
    try:
        states = state_register_db.get_all_states(session_id)
        for key in states:
            _ = state_register_db.delete_state(session_id, key)
        logger.debug(f"Cleared {len(states)} state_register_db variable(s) for session_id={session_id}")
    except Exception:
        logger.exception(f"Failed to clear state_register_db for session_id={session_id}")