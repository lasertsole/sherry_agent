"""The shared MesMemory connection is safe to use from several threads.

The store deliberately shares ONE ``sqlite3.Connection`` (``check_same_thread=False``)
and offloads work with ``asyncio.to_thread``, so concurrent tool-result flushes
reach ``execute`` from different threads. Before the connection was serialized,
the loser of that race raised

    sqlite3.InterfaceError: You can only execute one statement at a time

which tool-result persistence logs and swallows (fail open) — losing a row
silently. Observed on CI as two rows for three concurrent tool results, with the
traceback captured by the test that hit it.

The regression test is deterministic rather than a race the suite hopes to lose:
it wraps a connection that REPORTS overlapping execution, so it fails whenever
the serialization is missing, on any machine, and passes whenever it is present.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import context_engine.store.core as store_core
import context_engine.store.db as store_db

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]

_THREADS = 4
_CALLS_PER_THREAD = 40
# Long enough that unsynchronized callers overlap reliably, short enough that the
# deterministic test stays fast (40 calls x 4 threads x 1 ms).
_WINDOW_S = 0.001


class _OverlapDetectingConnection:
    """A connection double that records concurrent ``execute`` calls."""

    row_factory = sqlite3.Row
    total_calls = 0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inside = 0
        self.overlaps: list[int] = []

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._inside += 1
            self.total_calls += 1
            if self._inside > 1:
                self.overlaps.append(self._inside)
        time.sleep(_WINDOW_S)
        with self._lock:
            self._inside -= 1

        class _Cursor:
            def fetchone(self) -> tuple[int]:
                return (1,)

        return _Cursor()


def _hammer(connection: Any) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(_THREADS)

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(_CALLS_PER_THREAD):
                connection.execute("SELECT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, name=f"probe-{i}") for i in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return errors


def test_the_proxy_serializes_statements():
    """The invariant the fix provides: one statement at a time, always."""
    probe = _OverlapDetectingConnection()
    wrapped = store_db._SerializedConnection(probe)  # type: ignore[arg-type]

    errors = _hammer(wrapped)

    assert errors == [], errors
    assert probe.total_calls == _THREADS * _CALLS_PER_THREAD
    assert probe.overlaps == [], (
        f"{len(probe.overlaps)} statement(s) ran concurrently — this is the state "
        f"that raises sqlite3.InterfaceError in production"
    )


def test_without_the_proxy_the_double_reports_the_overlap():
    """The control: the same hammer on the bare double DOES overlap.

    Without this, ``test_the_proxy_serializes_statements`` would pass even if the
    hammer never applied any concurrency at all.
    """
    probe = _OverlapDetectingConnection()

    _hammer(probe)

    assert probe.overlaps != [], "the hammer produced no concurrency to serialize"


@pytest.fixture()
def shared_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real shared connection on a tmp database, wrapped as ``get_db`` wraps it."""
    db_path = tmp_path / "concurrent.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", None)

    conn = store_core._shared_db()
    conn.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, payload TEXT)")
    yield conn
    conn.close()
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", None)


def test_the_shared_connection_is_the_serialized_one(shared_connection):
    """``get_db()`` must not hand out a bare connection."""
    assert isinstance(shared_connection, store_db._SerializedConnection)


def test_the_proxy_behaves_like_a_connection(shared_connection):
    """Call sites must not be able to tell the difference."""
    assert shared_connection.row_factory is sqlite3.Row
    assert shared_connection.execute("SELECT 1 AS one").fetchone()["one"] == 1

    with shared_connection:
        assert shared_connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0


def test_real_statements_survive_concurrent_writers(shared_connection):
    """A small real-workload pass: interleaved SELECT + multi-row INSERT."""
    errors: list[BaseException] = []
    barrier = threading.Barrier(_THREADS)

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            for step in range(5):
                shared_connection.execute("SELECT COUNT(*) FROM probe").fetchone()
                shared_connection.executemany(
                    "INSERT INTO probe (payload) VALUES (?)",
                    [(f"row-{index}-{step}-{i}",) for i in range(5)],
                )
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], errors
    count = shared_connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert count == _THREADS * 5 * 5
