import shutil
from pathlib import Path
from config import SESSIONS_DIR
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history

def _session_folder(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / session_id).as_posix()

async def clear_session(session_id: str) -> None:
    """删除整个会话文件夹"""
    path = Path(_session_folder(session_id))
    if path.exists() and path.is_dir():
        shutil.rmtree(path)

    # 清空sqlite checkpointer中特定session_id的聊天记录
    await delete_thread_history(session_id=session_id)