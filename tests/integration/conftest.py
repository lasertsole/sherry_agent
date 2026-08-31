"""Integration test conftest — no mocking, real imports only."""

import asyncio
import logging

import aiosqlite
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _close_aiosqlite_connections_at_teardown(monkeypatch):
    """Close every aiosqlite connection opened during a test, at teardown.

    Why: the real-LLM e2e spawn chain opens one real aiosqlite connection per
    child-agent checkpointer (``agent/checkpointer/async_sqlite_checkpointer.py``
    via ``agent/tools/subagent/spawn/core.py``) and production code never
    closes it. The aiosqlite worker thread (``_connection_worker_thread``,
    parked in its ``while True: tx.get()`` loop) is **non-daemon** and only
    terminates when ``Connection.close()`` enqueues ``_STOP_RUNNING_SENTINEL``
    — so a solo ``-m llm_e2e`` run that PASSES still hangs forever at
    interpreter shutdown, blocked in CPython's ``threading._shutdown`` join.

    Recording at the ``aiosqlite.connect`` level catches every connection
    created during a test regardless of where it was born (child agents run
    as asyncio tasks in this same process), including the checkpointer saver
    connections and any registry/pending-injection store connections.

    Closing at each test's teardown (instead of session end) bounds each
    connection's lifetime. Tests that open no connections (the hermetic
    completion e2e file, and in-suite runs where the child build is stubbed)
    record an empty list, so this fixture is a no-op for them.
    """
    opened: list[aiosqlite.Connection] = []
    real_connect = aiosqlite.connect

    def _recording_connect(*args, **kwargs):
        # aiosqlite.connect() returns the Connection object synchronously;
        # the connection (and its worker thread) materializes on first await.
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", _recording_connect)

    yield

    for conn in opened:
        try:
            # Connection.close() is idempotent (it early-returns when the
            # connection is already closed) and loop-agnostic in effect: it
            # only uses the ambient loop for its completion future, which the
            # connection's own worker thread resolves, and its finally block
            # enqueues the stop sentinel that terminates that worker.
            # asyncio.run() supplies a fresh loop because the test's
            # pytest-asyncio loop may already be closed at this point.
            asyncio.run(conn.close())
        except Exception:  # noqa: BLE001 — teardown must never mask test results
            logger.debug(
                "aiosqlite teardown close skipped (likely already closed)",
                exc_info=True,
            )
