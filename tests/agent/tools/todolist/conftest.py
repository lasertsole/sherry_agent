"""Fixtures for tests/agent/tools/todolist/.

``isolated_db`` points the todolist store module at a per-test tmp SQLite file
and resets its once-per-process init state, mirroring
``tests/agent/tools/taskflow/conftest.py`` (which in turn mirrors the subagent
blueprint). The real ``agent/tools/todolist/data/todos.db`` is never touched.

``build_main_tools_real`` is the stub-tolerant loader copied from the taskflow
conftest: when tests/agent/tools/subagent is collected in the same process its
conftest installs a no-op ``agent.tools.build_main_tools`` at import time, so
tests observing the REAL ``_MAIN_TOOLS_BUILDERS`` wiring load the real package
``__init__`` under a private name instead.
"""

import asyncio
import importlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())

_agent_tools_cache: Any = None


def _load_module_from_file(
    private_name: str,
    file_path: Path,
    *,
    is_package: bool = False,
) -> Any:
    """Load a real module file under a private sys.modules name."""
    spec = importlib.util.spec_from_file_location(
        private_name,
        file_path,
        submodule_search_locations=[str(file_path.parent)] if is_package else None,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot create import spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[private_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_agent_tools() -> Any:
    global _agent_tools_cache
    if _agent_tools_cache is None:
        _agent_tools_cache = _load_module_from_file(
            "_todolist_real_agent_tools",
            _ROOT / "agent" / "tools" / "__init__.py",
            is_package=True,
        )
    return _agent_tools_cache


def _fix_stub_run_async() -> None:
    """Make ``from pub.func import run_async`` work inside the stub regime.

    Mirrors tests/agent/tools/taskflow/conftest.py: the subagent conftest's
    PEP 562 ``pub.func`` stub shadows the re-export with the submodule object,
    so bind the real function back before importing the real agent.tools.
    """
    stub = sys.modules.get("pub.func")
    if stub is None:
        return
    if callable(getattr(stub, "run_async", None)):
        return
    real = _load_module_from_file(
        "_todolist_real_pubfunc_run_async",
        _ROOT / "pub" / "func" / "run_async.py",
    )
    setattr(stub, "run_async", real.run_async)


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the todolist store module at a tmp_path db and reset init state."""
    from agent.tools.todolist.registry import store_sqlite

    db_path = tmp_path / "todos.db"
    monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
    monkeypatch.setattr(store_sqlite, "_DB_PATH", db_path)
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    # Fresh lock per test: a contended acquire BINDS an asyncio.Lock to the
    # acquiring test's event loop, and pytest-asyncio creates a new loop per test.
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    return db_path


@pytest.fixture()
def build_main_tools_real() -> Callable[[], list]:
    """Call build_main_tools, robust against the unit-test stub regime.

    In a solo run of tests/agent/tools/todolist the real agent.tools package is
    importable and the call goes through normally. When tests/agent/tools/subagent
    is collected first in the same process, its conftest replaces
    ``agent.tools.build_main_tools`` with ``lambda: []``; in that case fall
    back to the real package __init__ loaded under a private name.
    """

    def _build() -> list:
        from agent.tools import build_main_tools

        tools = build_main_tools()
        if tools:
            return list(tools)
        _fix_stub_run_async()
        return list(_real_agent_tools().build_main_tools())

    return _build
