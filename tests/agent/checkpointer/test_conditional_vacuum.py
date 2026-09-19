"""Conditional VACUUM tests for ``ThreadSafeAsyncSqliteSaver.aclean_old_checkpoints``.

Covers the reclamation branch added after keep-latest pruning: the freelist
threshold gate, the VACUUM itself, the empty-table path, and fail-open when the
VACUUM raises.
"""

import io
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from loguru import logger

import agent.checkpointer.thread_safe_checkpointer as mod
import config
from agent.checkpointer.thread_safe_checkpointer import (
    ThreadSafeAsyncSqliteSaver,
)

pytestmark = [pytest.mark.module]

_PAYLOAD = "x" * 2048
_TEST_THRESHOLD = 32 * 1024


def _seed_checkpoint_db(
    db_path: Path,
    *,
    threads: int = 3,
    rows_per_thread: int = 120,
) -> None:
    """Create the two LangGraph tables and fill every thread with stale rows."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints ("
            "  thread_id TEXT NOT NULL,"
            "  checkpoint_ns TEXT NOT NULL DEFAULT '',"
            "  checkpoint_id TEXT NOT NULL,"
            "  payload TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE writes ("
            "  thread_id TEXT NOT NULL,"
            "  checkpoint_ns TEXT NOT NULL DEFAULT '',"
            "  checkpoint_id TEXT NOT NULL"
            ")"
        )
        for thread in range(threads):
            conn.executemany(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?)",
                [(f"t{thread}", "", f"c{thread}-{i}", _PAYLOAD) for i in range(rows_per_thread)],
            )
            conn.executemany(
                "INSERT INTO writes VALUES (?, ?, ?)",
                [(f"t{thread}", "", f"c{thread}-{i}") for i in range(rows_per_thread)],
            )


def _freelist_bytes(db_path: Path) -> int:
    """Read ``PRAGMA freelist_count × page_size`` from the file."""
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(count) * int(page_size)


async def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Run ``aclean_old_checkpoints`` against ``tmp_path`` as its SRC_DIR."""
    # Given: SRC_DIR points at the hermetic checkpoints directory.
    monkeypatch.setattr(config, "SRC_DIR", tmp_path)
    conn = await aiosqlite.connect(":memory:")
    saver = ThreadSafeAsyncSqliteSaver(conn)
    try:
        return await saver.aclean_old_checkpoints()
    finally:
        await saver.aclose()


class _VacuumFailer:
    """Aiosqlite connection proxy that raises on every ``VACUUM``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> "_VacuumFailer":
        await self._conn.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return bool(await self._conn.__aexit__(exc_type, exc, tb))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.lstrip().upper().startswith("VACUUM"):
            raise sqlite3.OperationalError("database is locked")
        return await self._conn.execute(sql, *args, **kwargs)


def test_vacuum_threshold_is_ten_megabytes() -> None:
    """The production freelist trigger stays at the specified 10 MB."""
    assert mod._VACUUM_THRESHOLD_BYTES == 10 * 1024 * 1024


@pytest.mark.asyncio
class TestConditionalVacuum:
    """Prune-then-VACUUM decision, threshold gate, and fail-open behavior."""

    async def test_vacuum_reclaims_when_freelist_exceeds_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a prune freeing more than the threshold, VACUUM shrinks the file."""
        # Given: a DB seeded well above the (test-scaled) threshold.
        monkeypatch.setattr(mod, "_VACUUM_THRESHOLD_BYTES", _TEST_THRESHOLD)
        db_path = tmp_path / "checkpoints" / "sqlite.db"
        db_path.parent.mkdir()
        _seed_checkpoint_db(db_path)
        size_before = db_path.stat().st_size

        # When: keep-latest pruning runs.
        result = await _clean(tmp_path, monkeypatch)

        # Then: all stale rows are gone, the freelist is fully reclaimed, and the
        # file shrank (a DELETE alone never shrinks a file under auto_vacuum=0).
        assert result == {"threads_kept": 3, "deleted_checkpoints": 357, "deleted_writes": 357}
        assert _freelist_bytes(db_path) == 0
        assert db_path.stat().st_size < size_before

    async def test_no_vacuum_when_freelist_below_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a freelist under the default 10 MB, VACUUM must not run."""
        # Given: a small DB whose freed space stays far below 10 MB.
        db_path = tmp_path / "checkpoints" / "sqlite.db"
        db_path.parent.mkdir()
        _seed_checkpoint_db(db_path)
        size_before = db_path.stat().st_size

        # When: keep-latest pruning runs.
        result = await _clean(tmp_path, monkeypatch)

        # Then: pages stay parked on the freelist and the file is untouched —
        # a VACUUM would have reset the freelist to 0 and rewritten the file.
        assert result["deleted_checkpoints"] == 357
        assert _freelist_bytes(db_path) > 0
        assert db_path.stat().st_size == size_before

    async def test_vacuum_runs_when_checkpoints_table_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given an emptied table and a large freelist, VACUUM still reclaims."""
        # Given: a DB whose checkpoints were deleted outside the prune path.
        monkeypatch.setattr(mod, "_VACUUM_THRESHOLD_BYTES", _TEST_THRESHOLD)
        db_path = tmp_path / "checkpoints" / "sqlite.db"
        db_path.parent.mkdir()
        _seed_checkpoint_db(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM checkpoints")
            conn.execute("DELETE FROM writes")
        assert _freelist_bytes(db_path) > _TEST_THRESHOLD

        # When: the prune finds no checkpoints to keep.
        result = await _clean(tmp_path, monkeypatch)

        # Then: the prune is a no-op but the freelist is reclaimed anyway.
        assert result == {"threads_kept": 0, "deleted_checkpoints": 0, "deleted_writes": 0}
        assert _freelist_bytes(db_path) == 0

    async def test_vacuum_failure_is_fail_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a raising VACUUM, the prune result stands and a warning is logged."""
        # Given: pruning frees more than the threshold, but VACUUM is broken.
        monkeypatch.setattr(mod, "_VACUUM_THRESHOLD_BYTES", _TEST_THRESHOLD)
        db_path = tmp_path / "checkpoints" / "sqlite.db"
        db_path.parent.mkdir()
        _seed_checkpoint_db(db_path)
        monkeypatch.setattr(config, "SRC_DIR", tmp_path)

        # The saver's own connection is opened before connect() gets stubbed.
        real_connect = aiosqlite.connect
        saver = ThreadSafeAsyncSqliteSaver(await real_connect(":memory:"))

        def failing_connect(*args: Any, **kwargs: Any) -> _VacuumFailer:
            return _VacuumFailer(real_connect(*args, **kwargs))

        monkeypatch.setattr(aiosqlite, "connect", failing_connect)
        stream = io.StringIO()
        sink = logger.add(stream, format="{message}", level="WARNING")

        # When: keep-latest pruning runs against the failing VACUUM.
        try:
            result = await saver.aclean_old_checkpoints()
        finally:
            logger.remove(sink)
            await saver.aclose()

        # Then: the prune result is unaffected, only the latest row per thread
        # remains, and the failure is reported as a warning.
        assert result == {"threads_kept": 3, "deleted_checkpoints": 357, "deleted_writes": 357}
        with sqlite3.connect(db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        assert remaining == 3
        assert "checkpoint db vacuum skipped" in stream.getvalue()
