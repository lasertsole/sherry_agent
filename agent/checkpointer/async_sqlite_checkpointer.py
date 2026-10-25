import aiosqlite
from pathlib import Path
from config import SRC_DIR
from pub.func import rand_str_to_int
from agent.checkpointer.thread_safe_checkpointer import (
    ThreadSafeAsyncSqliteSaver,
)


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

    # async with guarantees the connection (and its file handle) is closed on
    # both success and failure paths — without it, every clear_session call
    # leaked one connection. Uncommitted work is rolled back on close.
    async with aiosqlite.connect(sqlite_file_path, check_same_thread=False) as _conn:
        await _conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        await _conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
        await _conn.commit()


async def prune_session_checkpoints(session_id: str) -> dict[str, int]:
    """Keep only the latest top-level checkpoint for one session's thread.

    Thin module-level seam for the turn-end cleanup: callers (server turn_runner)
    do not own a graph/checkpointer instance and must not import the saver's
    internals. Opens a short-lived connection; a missing DB is a no-op.
    """
    checkpoints_dir: Path = (SRC_DIR / "checkpoints").resolve()
    sqlite_file_path: Path = checkpoints_dir / "sqlite.db"
    if not sqlite_file_path.exists():
        return dict(kept=0, deleted_checkpoints=0, deleted_writes=0)

    conn = await aiosqlite.connect(sqlite_file_path, check_same_thread=False)
    try:
        saver = ThreadSafeAsyncSqliteSaver(conn)
        return await saver.aclean_thread_checkpoints(str(rand_str_to_int(session_id)))
    finally:
        await conn.close()
