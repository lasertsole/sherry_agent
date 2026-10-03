"""Shared fixtures for tests/server: simulate the boot-time runtime-hook assembly.

``server/__main__.py`` registers the server-owned callbacks into
``runtime.hooks`` at boot and the agent layer resolves them at call time.
Server tests that drive the real services — e.g. the ``auto_turn_inflight``
signal of ``agent.tools.subagent.registry.session_state`` — need that same
assembly, so the real auto-turn module is registered as the
``AUTO_TURN_MODULE`` hook for every test here and removed again on teardown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from runtime import hooks


@pytest.fixture(autouse=True)
def _auto_turn_module_hook() -> Iterator[None]:
    """Register the real auto-turn module as the runtime hook (server assembled)."""
    from server.service import auto_turn as auto_turn_module

    hooks.register(hooks.AUTO_TURN_MODULE, lambda: auto_turn_module)
    yield
    hooks.unregister(hooks.AUTO_TURN_MODULE)


@pytest.fixture()
def isolated_auth_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the auth store at a tmp db and reset its once-per-process init state.

    Same isolation contract as the other SQLite-store suites: the real
    ``src/data/auth.db`` is never touched, and a fresh lock keeps a contended
    acquire from binding to the previous test's event loop.
    """
    from server.DAO import auth_store
    from server.service import auth_service

    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_store, "_DB_DIR", tmp_path)
    monkeypatch.setattr(auth_store, "_DB_PATH", db_path)
    monkeypatch.setattr(auth_store, "_initialized", False)
    monkeypatch.setattr(auth_store, "_init_loop", None)
    monkeypatch.setattr(auth_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(auth_store, "_sync_tables_ready", False)
    # The gate caches "enabled/has_account"; a stale cache across tests would
    # make a fresh store look like the previous one.
    auth_service.clear_state_cache()
    yield db_path
    auth_service.clear_state_cache()


@pytest.fixture(autouse=True)
def _never_touch_the_real_auth_db(isolated_auth_store) -> Iterator[None]:
    """Every server test runs against a tmp auth DB.

    The store's default path is the developer's ``src/data/auth.db``; a test that
    reaches the auth service without asking for the isolation fixture (the push
    channel's WS gate, for instance) would otherwise create tables in it — and a
    future test could write a row there.
    """
    yield
