import shutil
from pathlib import Path
from config import SESSIONS_DIR


def _session_folder(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / session_id).as_posix()

def clear_session(session_id: str) -> None:
    """删除整个会话文件夹"""
    path = Path(_session_folder(session_id))
    if path.exists() and path.is_dir():
        shutil.rmtree(path)