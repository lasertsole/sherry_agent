"""TDD tests for audit issue #15 — thread-safe ``get_db()`` singleton.

Audit findings being pinned by these tests:

* ``context_engine/store/db.py`` used an unlocked ``if _db:`` double-check, so
  two threads racing on a cold singleton could each create a connection and
  the second would overwrite (leak) the first.
* ``timeout=1.0`` is too short under contention: concurrent access raises
  ``sqlite3.OperationalError: database is locked`` with no retry.

Contract after the fix:

1. Concurrent first calls return ONE shared connection (exactly one
   ``sqlite3.connect`` call), even when connect is artificially slow.
2. A transient "database is locked" OperationalError during connect/migrate
   is retried with backoff instead of failing the caller.
3. The connection is created with a busy timeout of at least 5 seconds.
"""

import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

import context_engine.store.db as db_mod

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


@pytest.fixture
def fresh_singleton():
    """Reset the module singleton state around a test, restoring afterwards."""
    saved_db = db_mod._db
    saved_lock = getattr(db_mod, "_db_lock", None)
    db_mod._db = None
    try:
        yield db_mod
    finally:
        db_mod._db = saved_db
        if saved_lock is not None:
            db_mod._db_lock = saved_lock


class TestGetDbSingleton:
    def test_concurrent_first_calls_share_one_connection(self, fresh_singleton):
        """N threads racing on a cold singleton must produce ONE connection.

        The artificial sleep inside sqlite3.connect widens the race window so
        the old unlocked double-check deterministically created two
        connections (one leaked).
        """
        real_connect = sqlite3.connect
        connect_calls: list[sqlite3.Connection] = []
        connect_lock = threading.Lock()

        def slow_connect(*args, **kwargs):
            time.sleep(0.1)  # widen the race window
            conn = real_connect(*args, **kwargs)
            with connect_lock:
                connect_calls.append(conn)
            return conn

        barrier = threading.Barrier(8)
        results: list[sqlite3.Connection] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()  # all threads hit get_db() at the same moment
            db = db_mod.get_db()
            with results_lock:
                results.append(db)

        with patch.object(db_mod.sqlite3, "connect", side_effect=slow_connect):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        assert len(results) == 8, "all workers must return"
        assert len({id(db) for db in results}) == 1, (
            "all threads must observe the SAME connection object"
        )
        assert len(connect_calls) == 1, (
            f"exactly one sqlite3.connect call expected, got {len(connect_calls)} "
            "(a second connection means the old one leaked)"
        )
        # The singleton now holds the same object the threads observed.
        assert db_mod._db is results[0]

        # Close the temp connection to release WAL files.
        results[0].close()

    def test_retry_on_database_locked(self, fresh_singleton, monkeypatch):
        """A transient 'database is locked' on connect must be retried."""
        real_connect = sqlite3.connect
        attempts: list[int] = []

        def flaky_connect(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(db_mod.sqlite3, "connect", flaky_connect)
        monkeypatch.setattr(db_mod, "_CONNECT_ATTEMPTS", 3, raising=False)
        monkeypatch.setattr(db_mod, "_RETRY_DELAY_S", 0.01, raising=False)

        db = db_mod.get_db()

        assert db is not None
        assert len(attempts) >= 2, "expected at least one retry after the locked error"
        db.close()

    def test_non_locked_operational_error_not_retried(self, fresh_singleton, monkeypatch):
        """Unrelated OperationalError (e.g. disk I/O error) fails immediately."""
        attempts: list[int] = []

        def broken_connect(*args, **kwargs):
            attempts.append(1)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db_mod.sqlite3, "connect", broken_connect)
        monkeypatch.setattr(db_mod, "_RETRY_DELAY_S", 0.01, raising=False)

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            db_mod.get_db()

        assert len(attempts) == 1, "non-lock errors must not be retried"

    def test_busy_timeout_at_least_five_seconds(self, fresh_singleton):
        """The connection must use a busy timeout >= 5s (old value was 1.0)."""
        real_connect = sqlite3.connect
        captured_kwargs: dict = {}

        def capturing_connect(path, *args, **kwargs):
            captured_kwargs.update(kwargs)
            return real_connect(path, *args, **kwargs)

        with patch.object(db_mod.sqlite3, "connect", side_effect=capturing_connect):
            db = db_mod.get_db()

        assert captured_kwargs.get("timeout", 0) >= 5.0, (
            f"busy timeout must be >= 5.0s, got {captured_kwargs.get('timeout')}"
        )
        db.close()
