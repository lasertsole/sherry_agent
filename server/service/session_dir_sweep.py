"""Boot-time reconciliation of session folders against the stores.

``clear_session`` removes the whole ``SESSIONS_DIR/<session_id>/`` tree, but a
crash mid-purge (or a purge that deleted the rows and failed on the folder) can
leave a directory nobody references: no messages, no todos, no flows, no
checkpoints. Those folders hold evicted tool results and offloaded media, so they
are exactly the ones worth reclaiming — and nothing else ever looks at them.

The scan runs at boot (the moment a crash's leftovers are around) and is built to
be conservative: a folder is only removed when EVERY store says it is unknown, so
a read failure, a fresh directory, or a session that only ever wrote todos is
left alone.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from loguru import logger
from pub.func.validator import is_safe_session_id

from config.features import SESSION_DIRS
from config.path import SESSIONS_DIR

__all__ = ["sweep_orphaned_session_dirs"]


def _count_files(directory: Path) -> int:
    """Recursively count files under ``directory`` (0 when unreadable)."""
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for path in directory.rglob("*") if path.is_file())
    except OSError:
        return 0


def _messages_has_session(session_id: str) -> bool:
    try:
        from context_engine.store.db import get_db

        cursor = get_db().execute(
            "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)
        )
        return cursor.fetchone() is not None
    except Exception:
        logger.debug("session-dir sweep: messages probe failed", exc_info=True)
        return True  # fail-open: an unreadable store means "keep"


def _todos_has_session(session_id: str) -> bool:
    try:
        import sqlite3

        from agent.tools.todolist.registry.store_sqlite import _DB_PATH

        if not Path(_DB_PATH).exists():
            return False
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            cursor = conn.execute("SELECT 1 FROM todos WHERE session_id = ? LIMIT 1", (session_id,))
            return cursor.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        logger.debug("session-dir sweep: todos probe failed", exc_info=True)
        return True


def _taskflows_has_session(session_id: str) -> bool:
    try:
        import sqlite3

        from agent.tools.taskflow.registry.store_sqlite import _DB_PATH

        if not Path(_DB_PATH).exists():
            return False
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            cursor = conn.execute(
                "SELECT 1 FROM task_flows WHERE session_id = ? LIMIT 1", (session_id,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        logger.debug("session-dir sweep: taskflow probe failed", exc_info=True)
        return True


def _checkpoints_has_session(session_id: str) -> bool:
    try:
        import sqlite3

        from config.path import SRC_DIR

        db_path = Path((SRC_DIR / "checkpoints" / "sqlite.db").resolve())
        if not db_path.exists():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (session_id,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        logger.debug("session-dir sweep: checkpoint probe failed", exc_info=True)
        return True


def _session_has_any_records(session_id: str) -> bool:
    """True when ANY store still knows this session (fail-open per store)."""
    return any(
        probe(session_id)
        for probe in (
            _messages_has_session,
            _todos_has_session,
            _taskflows_has_session,
            _checkpoints_has_session,
        )
    )


def sweep_orphaned_session_dirs() -> int:
    """Remove session folders no store references. Returns how many were removed.

    Never raises: this runs at boot and a failure must not stop the server.
    """
    if not SESSION_DIRS["boot_sweep_enabled"]:
        return 0
    try:
        return _do_sweep()
    except Exception:
        logger.warning("orphaned session-dir sweep failed", exc_info=True)
        return 0


def _do_sweep() -> int:
    base = Path(SESSIONS_DIR)
    if not base.is_dir():
        return 0

    min_age = int(SESSION_DIRS["min_dir_age_seconds"])
    now = time.time()
    cleaned = 0

    for session_dir in sorted(base.iterdir()):
        # A symlink is never followed: rmtree on one would delete the TARGET.
        if session_dir.is_symlink():
            logger.warning("session-dir sweep: skipping symlink {}", session_dir)
            continue
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        if not is_safe_session_id(session_id):
            logger.warning("session-dir sweep: skipping unsafe name {}", session_id)
            continue
        try:
            age = now - session_dir.stat().st_mtime
        except OSError:
            continue
        if age < min_age:
            continue  # freshly created: the session may still be initializing
        if _session_has_any_records(session_id):
            continue

        evicted = _count_files(session_dir / "evicted")
        media = _count_files(session_dir / "media")
        logger.info(
            "session-dir sweep: removing orphaned session dir (no store records, "
            "age={:.0f}s): {} ({} evicted, {} media file(s))",
            age,
            session_id,
            evicted,
            media,
        )
        shutil.rmtree(session_dir, ignore_errors=True)
        cleaned += 1

    return cleaned
