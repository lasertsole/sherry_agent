"""The memory transaction holds the shared statement lock for its whole span.

Per-statement locking lets a concurrent reader land between two INSERTs on the
same MesMemory connection and observe a half-applied write. ``transaction()``
now takes the proxy's ``begin_transaction``/``end_transaction`` hold; the private
(test) connection path still works without the proxy.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from context_engine.store.db import _SerializedConnection
from context_engine.store.memory_entry_repository import MemoryEntryRepository

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_ORIGINAL_DB = MemoryEntryRepository.db


def test_the_proxy_holds_the_statement_lock_across_a_transaction():
    proxy = _SerializedConnection(
        sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    )
    proxy.execute("CREATE TABLE t (x INTEGER)")
    ran = threading.Event()

    def _reader() -> None:
        proxy.execute("SELECT 1")
        ran.set()

    proxy.begin_transaction()
    try:
        proxy.execute("BEGIN IMMEDIATE")
        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        # The reader cannot run while the holder owns the lock.
        assert ran.wait(0.1) is False
        proxy.execute("INSERT INTO t VALUES (1)")
        proxy.execute("COMMIT")
    finally:
        proxy.end_transaction()

    assert ran.wait(1.0) is True
    assert proxy.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    proxy.close()


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str, params: tuple = ()):  # noqa: ARG002 - fake surface
        self.calls.append(sql)
        return self

    def begin_transaction(self) -> None:
        self.calls.append("BEGIN_HOLD")

    def end_transaction(self) -> None:
        self.calls.append("END_HOLD")


def _repo_with(fake: _FakeConn) -> MemoryEntryRepository:
    repo = MemoryEntryRepository()
    monkeypatch_property = property(lambda _self: fake)
    type(repo).db = monkeypatch_property  # type: ignore[method-assign]
    return repo


def test_the_repository_orders_begin_commit_and_release():
    fake = _FakeConn()
    repo = _repo_with(fake)
    try:
        with repo.transaction() as conn:
            assert conn is fake
    finally:
        type(repo).db = _ORIGINAL_DB  # type: ignore[method-assign]

    assert fake.calls == ["BEGIN_HOLD", "BEGIN IMMEDIATE", "COMMIT", "END_HOLD"]


def test_the_repository_releases_and_rolls_back_on_error():
    fake = _FakeConn()
    repo = _repo_with(fake)
    try:
        with pytest.raises(RuntimeError):
            with repo.transaction():
                raise RuntimeError("boom")
    finally:
        type(repo).db = _ORIGINAL_DB  # type: ignore[method-assign]

    assert fake.calls == ["BEGIN_HOLD", "BEGIN IMMEDIATE", "ROLLBACK", "END_HOLD"]


def test_a_private_connection_transaction_still_commits(tmp_path):
    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    try:
        with repo.transaction() as conn:
            conn.execute(
                "INSERT INTO memory_entries (target, content, created_at, char_count, archived) "
                "VALUES ('memory', 'hello', 1, 5, 0)"
            )
        assert repo.count("memory") == 1
    finally:
        repo.close()
