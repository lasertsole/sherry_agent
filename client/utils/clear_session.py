from pathlib import Path
from config import SRC_DIR
from .chats_storage import ChatStorage

def _session_folder(session_id: str) -> str:
    return (Path(SRC_DIR) / f"session/{session_id}").as_posix()

def clear_session(session_id: str) -> None:
    chat_storage: ChatStorage = ChatStorage(session_id=session_id, chats_maxlen=20)
    chat_storage.clear_chats()