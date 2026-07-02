import aiosqlite
from pathlib import Path
from config import SRC_DIR
from pub_func import rand_str_to_int
from agent.checkpointer.thread_safe_checkpointer import (
    ThreadSafeAsyncSqliteSaver,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


# Create async sqlite checkpointer
async def build_async_sqlite_checkpointer() -> ThreadSafeAsyncSqliteSaver:
    checkpoints_dir: Path = (SRC_DIR / "checkpoints").resolve()
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file_path: Path = checkpoints_dir / "sqlite.db"

    conn: aiosqlite.Connection = await aiosqlite.connect(sqlite_file_path, check_same_thread=False)
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA cache_size=-64000")

    checkpointer = ThreadSafeAsyncSqliteSaver(conn)

    return checkpointer

# Delete all chat records for a given session_id
async def delete_thread_history(session_id: str) -> None:
    """Delete all chat records (checkpoints + writes) for the given thread_id."""
    thread_id: int = rand_str_to_int(session_id)

    checkpoints_dir: Path = (SRC_DIR / "checkpoints").resolve()
    sqlite_file_path: Path = checkpoints_dir / "sqlite.db"

    _conn = await aiosqlite.connect(sqlite_file_path, check_same_thread=False)
    await _conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
    await _conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
    await _conn.commit()