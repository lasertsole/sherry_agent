"""The PRAGMA one-liner every checkpointer connection needs.

``journal_mode = WAL`` is a property of the DATABASE header — set once and every
later connection inherits it. ``busy_timeout`` is a property of the CONNECTION:
each new connection starts at zero, so the second writer of a shared SQLite file
fails immediately with ``SQLITE_BUSY`` instead of waiting for the first one to
commit. The checkpointer opens short-lived connections all over (global prune,
per-thread prune, delete, purge) while the graph's own connection is mid-turn,
so a missing timeout turns ordinary concurrency into failed turns and half-done
purges.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

__all__ = ["BUSY_TIMEOUT_MS", "connect_with_busy_timeout"]

#: How long a writer waits for the write lock before giving up. The longest
#: legitimate writer here is a compaction-sized checkpoint write, measured well
#: under a second; 30 s is generous headroom that still bounds a wedged writer.
BUSY_TIMEOUT_MS = 30_000


async def connect_with_busy_timeout(db_path: str | Path) -> aiosqlite.Connection:
    """Open a connection that WAITS for the write lock instead of failing.

    The caller owns the result and must close it (``try/finally`` or
    ``async with``); a failed PRAGMA closes the connection before raising so a
    half-initialised handle cannot leak.
    """
    conn = await aiosqlite.connect(str(db_path), check_same_thread=False)
    try:
        await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except BaseException:
        await conn.close()
        raise
    return conn
