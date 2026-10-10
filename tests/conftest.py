"""Shared pytest fixtures for the EMA_AI_agent test suite.

Consolidates the former per-type-directory conftests (the pre-mirror ``unit`` and
``integration`` trees) after the mirror-structure migration: their autouse safety nets now apply
suite-wide. Markers are declared in ``pyproject.toml`` only — the hook that used to
re-register them here had drifted out of sync with that list.
"""

import asyncio
import faulthandler
import logging
import os
import threading
import time

import aiosqlite
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

logger = logging.getLogger(__name__)


@pytest.fixture
def unit_test_config():
    """Patch ROOT_DIR and AUTO_SKILLS_DIR to temp directories for isolated tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        with (
            patch("config.path.ROOT_DIR", tmp_path),
            patch("config.path.SRC_DIR", tmp_path / "src"),
            patch("config.path.AUTO_SKILLS_DIR", tmp_path / "skills" / "auto"),
            patch("config.path.WORKSPACE_DIR", tmp_path / "workspace"),
            patch("config.path.TEMP_DIR", tmp_path / "temp"),
            patch("config.path.MODELS_DIR", tmp_path / "models"),
            patch("config.path.SKILLS_DIR", tmp_path / "skills"),
        ):
            yield tmp_path


@pytest.fixture(autouse=True)
def _isolated_prompt_data_provider():
    """No test may leak a registered prompt data provider into the next test."""
    from runtime import data_provider

    yield
    data_provider.clear_prompt_data_provider()


@pytest.fixture(autouse=True)
def _isolated_skill_scan_cache(tmp_path):
    """Point the SkillSpector verdict cache at per-test tmp storage and freeze
    the scanner-version fingerprint.

    Freezing ``_scanner_version_fingerprint`` keeps unit tests fully hermetic
    (no real ``skillspector --version`` subprocess), makes cache keys
    deterministic, and prevents the probe from consuming patched
    ``subprocess.run`` calls that test_skill_scanner.py counts with
    ``assert_called_once``.
    """
    try:
        import server.service.skill_scanner  # noqa: F401
    except Exception:
        # PART2 §12: a broken skill_scanner import (e.g. langgraph
        # ExecutionInfo environment issues) must not crash every unit
        # test at fixture setup; degrade to no-patching so unrelated
        # tests keep running.
        logger.warning(
            "server.service.skill_scanner import failed; "
            "SkillSpector scan-cache isolation patches skipped",
            exc_info=True,
        )
        yield
        return
    with (
        patch(
            "server.service.skill_scanner._CACHE_PATH",
            tmp_path / "skills_scan_cache.json",
        ),
        patch(
            "server.service.skill_scanner._scanner_version_fingerprint",
            return_value="SkillSpector v-unit-stable",
        ),
    ):
        yield


#: Every aiosqlite Connection object this session's recorder saw, in creation
#: order — the session-end sweep re-closes them because aiosqlite starts its
#: worker THREAD only on first await: a connection created in one test and first
#: awaited in a later one is closed (as a no-op) by the wrong test's teardown and
#: would otherwise outlive the session as a live non-daemon thread.
_RECORDED_AIOSQLITE: list[object] = []


def _close_recorded_aiosqlite_connections() -> None:
    """Close every recorded connection (idempotent — close() re-closes safely).

    ``close()`` early-returns when the connection never finished connecting, so
    a worker whose awaiting task was cancelled keeps its thread parked forever.
    ``stop()`` is the thread-stopping half of that pair and is what actually
    releases such a worker (it enqueues the sentinel the loop exits on).
    """
    while _RECORDED_AIOSQLITE:
        conn = _RECORDED_AIOSQLITE.pop()
        try:
            asyncio.run(conn.close())
        except Exception:  # noqa: BLE001 - the sweep must never mask results
            logger.debug("aiosqlite session sweep close skipped", exc_info=True)
        thread = getattr(conn, "_thread", None)
        if thread is not None and thread.is_alive():
            try:
                conn.stop()
            except Exception:  # noqa: BLE001 - same contract as above
                logger.debug("aiosqlite session sweep stop skipped", exc_info=True)


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
        _RECORDED_AIOSQLITE.append(conn)
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


def _executor_worker_threads() -> set[threading.Thread]:
    """Threads owned by a ThreadPoolExecutor, which cannot hang the exit.

    Their workers are non-daemon, but ``concurrent.futures.thread`` registers a
    ``threading`` atexit handler that wakes every idle worker *before* the
    interpreter's join runs — so an idle ``asyncio_0`` worker is not a leak,
    and only genuinely abandoned threads are.
    """
    from concurrent.futures import thread as futures_thread

    queues = getattr(futures_thread, "_threads_queues", None)
    return set(queues) if queues else set()


def pytest_sessionfinish(session, exitstatus) -> None:
    """No session may end with live non-daemon threads.

    CPython's exit joins every non-daemon thread before the interpreter can
    finish, so a single leaked worker turns a fully *passing* session into a
    process that prints its summary and then hangs forever — in CI that burned
    a whole 30-minute job with group A's "5269 passed" as the final log line
    (neither an aiosqlite connection nor an snkv store had been closed; see
    ``_close_aiosqlite_connections_at_teardown`` above for the per-test fix).

    Anything that still slips through is reported with its stacks, and the
    process hard-exits after a short grace so a test leak can never become a
    hung job.
    """
    _close_recorded_aiosqlite_connections()
    executor_workers = _executor_worker_threads()
    survivors = [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread()
        and not thread.daemon
        and thread not in executor_workers
    ]
    if not survivors:
        return

    print(
        f"\n[tests/conftest] {len(survivors)} non-daemon thread(s) outlived the session: "
        f"{[thread.name for thread in survivors]}\n"
        "Whoever opened them must close them (see _close_aiosqlite_connections_at_teardown "
        "for the pattern); exiting anyway so they cannot hang the process.",
        flush=True,
    )
    faulthandler.dump_traceback(all_threads=True)

    status = int(exitstatus)

    def _exit_when_reported() -> None:
        # Grace for the terminal summary (and any coverage write-out) to reach
        # the log before the process is taken down.
        time.sleep(3.0)
        os._exit(status)

    threading.Thread(target=_exit_when_reported, name="session-hard-exit", daemon=True).start()
