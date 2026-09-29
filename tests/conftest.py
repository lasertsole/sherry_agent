"""Shared pytest fixtures for the EMA_AI_agent test suite.

Consolidates the former per-type-directory conftests (the pre-mirror ``unit`` and
``integration`` trees) after the mirror-structure migration: their autouse safety nets now apply
suite-wide. Markers are declared in ``pyproject.toml`` only — the hook that used to
re-register them here had drifted out of sync with that list.
"""

import asyncio
import logging

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
