"""Shared pytest fixtures for the EMA_AI_agent test suite.

Consolidates the former per-type-directory conftests (tests/unit, tests/integration)
after the mirror-structure migration: their autouse safety nets now apply
suite-wide.
"""

import asyncio
import logging

import aiosqlite
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

logger = logging.getLogger(__name__)


def pytest_configure(config):
    """Register custom markers and set asyncio mode."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "module: mark test as module test")
    config.addinivalue_line("markers", "system: mark test as system test")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "regression: mark test as regression test")
    config.addinivalue_line(
        "markers",
        "llm_e2e: real-LLM network e2e test (slow, costs tokens, order-sensitive; "
        "deselected by default — run explicitly with `-m llm_e2e`)",
    )


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


@pytest.fixture
def tmp_skills_dir(unit_test_config):
    """Create a temp auto-skills directory with test SKILL.md files."""
    skills_dir = unit_test_config / "skills" / "auto"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Create a test skill
    test_skill_dir = skills_dir / "test_skill"
    test_skill_dir.mkdir()
    (test_skill_dir / "SKILL.md").write_text(
        "---\nname: test_skill\ndescription: A test skill\n---\n\nThis is a test skill body.",
        encoding="utf-8",
    )

    return skills_dir


@pytest.fixture
def message_bus():
    """Create a fresh MessageBus instance for bus tests."""
    from bus.core import MessageBus

    return MessageBus()


@pytest.fixture(autouse=True)
def clean_registers():
    """Clear all register sessions before each test to prevent state leakage."""
    yield
    # Cleanup after test if needed


@pytest.fixture
def mock_state_register_mem():
    """Provide a clean StateRegisterMeM instance for module tests."""
    from runtime.state_register import StateRegisterMeM

    # Force a fresh instance by clearing the singleton
    from runtime.core import Register

    if StateRegisterMeM in Register._instances:
        del Register._instances[StateRegisterMeM]
    reg = StateRegisterMeM()
    yield reg
    # Cleanup: clear all sessions
    for session_id in list(reg._states.keys()):
        reg.clear_session(session_id)


@pytest.fixture
def tmp_sqlite_db():
    """Create a temporary SQLite database for StateRegisterDB tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_state.db"
        yield db_path


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
