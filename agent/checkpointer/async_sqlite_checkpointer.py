import aiosqlite
from pathlib import Path
from config import SRC_DIR
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async def build_async_sqlite_checkpointer()-> AsyncSqliteSaver:
    checkpoints_dir: Path = (SRC_DIR / "checkpoints").resolve()
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file_path: Path = checkpoints_dir / "sqlite.db"

    conn = await aiosqlite.connect(sqlite_file_path, check_same_thread=False)
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA cache_size=-64000")

    checkpointer = AsyncSqliteSaver(conn)

    return checkpointer