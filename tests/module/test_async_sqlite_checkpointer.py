"""Module tests for agent/checkpointer/async_sqlite_checkpointer.py.

Focus: delete_thread_history must never leak its aiosqlite connection
(audit #10) and must delete rows for exactly the target thread.
"""

import sqlite3
from unittest.mock import MagicMock

import aiosqlite
import pytest

from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from pub_func import rand_str_to_int


class FakeConnection:
    """Minimal aiosqlite.Connection stand-in recording close() calls.

    Mirrors the real shape: aiosqlite.connect() is a SYNC factory returning
    an object that is itself the async context manager.
    """

    def __init__(self, fail_on_execute: str | None = None):
        self.closed = False
        self.executed: list[str] = []
        self._fail_on_execute = fail_on_execute

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.close()
        return False  # never swallow exceptions

    async def execute(self, sql: str, *args, **kwargs) -> MagicMock:
        self.executed.append(sql)
        if self._fail_on_execute is not None and self._fail_on_execute in sql:
            raise sqlite3.OperationalError("boom")
        return MagicMock()

    async def commit(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
class TestDeleteThreadHistory:
    """delete_thread_history connection lifecycle + deletion behavior."""

    @pytest.fixture
    def fake_connect(self, monkeypatch):
        """Patch module-level aiosqlite.connect; return the list of created conns."""
        created: list[FakeConnection] = []

        def _connect(*args, **kwargs):
            conn = FakeConnection()
            created.append(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", _connect)
        return created

    async def test_connection_closed_after_delete(self, fake_connect):
        """Success path: the connection must be closed (audit #10 leak regression)."""
        await delete_thread_history("session_abc")

        assert len(fake_connect) == 1
        assert fake_connect[0].closed is True
        # Both tables were targeted.
        assert any("DELETE FROM checkpoints" in sql for sql in fake_connect[0].executed)
        assert any("DELETE FROM writes" in sql for sql in fake_connect[0].executed)

    async def test_connection_closed_on_execute_error(self, monkeypatch):
        """Failure path: a failing DELETE must still close the connection."""
        created: list[FakeConnection] = []

        def _connect(*args, **kwargs):
            conn = FakeConnection(fail_on_execute="writes")
            created.append(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", _connect)

        with pytest.raises(sqlite3.OperationalError):
            await delete_thread_history("session_abc")

        assert created[0].closed is True

    async def test_deletes_only_target_thread_rows(self, tmp_path, monkeypatch):
        """End-to-end against a real temp sqlite.db: only the target thread's rows go."""
        # Redirect the module to a hermetic checkpoints dir (module imports
        # SRC_DIR by name, so patch the module attribute).
        import agent.checkpointer.async_sqlite_checkpointer as mod

        checkpoints_dir = tmp_path / "checkpoints"
        checkpoints_dir.mkdir()
        db_path = checkpoints_dir / "sqlite.db"
        monkeypatch.setattr(mod, "SRC_DIR", tmp_path)

        thread_id = rand_str_to_int("session_abc")
        other_id = rand_str_to_int("session_other")

        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE checkpoints (thread_id INTEGER)")
            conn.execute("CREATE TABLE writes (thread_id INTEGER)")
            conn.executemany(
                "INSERT INTO checkpoints VALUES (?)",
                [(thread_id,), (thread_id,), (other_id,)],
            )
            conn.executemany(
                "INSERT INTO writes VALUES (?)",
                [(thread_id,), (other_id,), (other_id,)],
            )

        await delete_thread_history("session_abc")

        with sqlite3.connect(db_path) as conn:
            remaining_checkpoints = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            remaining_writes = conn.execute(
                "SELECT COUNT(*) FROM writes WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            other_checkpoints = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (other_id,)
            ).fetchone()[0]
            other_writes = conn.execute(
                "SELECT COUNT(*) FROM writes WHERE thread_id = ?", (other_id,)
            ).fetchone()[0]

        assert remaining_checkpoints == 0
        assert remaining_writes == 0
        assert other_checkpoints == 1  # untouched session keeps its rows
        assert other_writes == 2
