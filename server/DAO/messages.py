import shutil
from pathlib import Path
from config import SESSIONS_DIR
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history

def _session_folder(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / session_id).as_posix()

async def clear_session(session_id: str) -> None:
    """Delete the entire session folder"""
    path = Path(_session_folder(session_id))
    if path.exists() and path.is_dir():
        shutil.rmtree(path)

    # Clear chat history for this session_id from the sqlite checkpointer
    await delete_thread_history(session_id=session_id)