import aiosqlite
from pathlib import Path
from config import SRC_DIR
from pub_func import rand_str_to_int
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_conn: aiosqlite.Connection | None = None

# 创建异步sqlite保存器
async def build_async_sqlite_checkpointer() -> AsyncSqliteSaver:
    checkpoints_dir: Path = (SRC_DIR / "checkpoints").resolve()
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file_path: Path = checkpoints_dir / "sqlite.db"

    _conn = await aiosqlite.connect(sqlite_file_path, check_same_thread=False)
    await _conn.execute("PRAGMA journal_mode = WAL")
    await _conn.execute("PRAGMA foreign_keys = ON")
    await _conn.execute("PRAGMA cache_size=-64000")

    checkpointer = AsyncSqliteSaver(_conn)

    return checkpointer

# 删除指定session_id 的所有聊天记录
async def delete_thread_history(session_id: str) -> None:
    """删除指定 thread_id 的所有聊天记录（checkpoints + writes）。"""
    thread_id: int = rand_str_to_int(session_id)

    checkpoints_dir: Path = (SRC_DIR / "checkpoints").resolve()
    sqlite_file_path: Path = checkpoints_dir / "sqlite.db"

    _conn = await aiosqlite.connect(sqlite_file_path, check_same_thread=False)
    await _conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
    await _conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
    await _conn.commit()