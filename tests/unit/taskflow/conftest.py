"""Fixtures and helpers for tests/unit/taskflow/.

Two concerns are shared here:

1. ``isolated_db``: point the taskflow store module at a per-test tmp SQLite
   file and reset its once-per-process init state (mirrors the pattern in
   tests/unit/subagent/test_store_sqlite.py).

2. Stub-tolerant loaders for REAL modules: when tests/unit/subagent is
   collected in the same pytest process, its conftest installs sys.modules
   stubs at import time (including ``skills.loader`` and a no-op
   ``agent.tools.build_main_tools``). Tests that must observe REAL behavior
   (skill file discovery, the real _MAIN_TOOLS_BUILDERS wiring) therefore load
   the real source files under private module names, mirroring the
   ``_real_init_attr`` pattern established in tests/unit/subagent/conftest.py.
"""

import asyncio
import importlib
import importlib.util
import sys
import types as stdlib_types
from pathlib import Path
from typing import Any, Callable

import pytest

_ROOT = Path(__file__).resolve().parents[3]

_skills_loader_cache: Any = None
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


def _real_skills_loader() -> Any:
    """Load the real skills/loader.py under a private package name.

    The loader uses a call-time relative import (``from .skills_snapshot
    import read_skills_snapshot``), so it must live inside a package context
    for that to resolve: a private parent package whose ``__path__`` points
    at the real skills directory gives the loader (and skills_snapshot) a
    fully real, stub-free import neighborhood without touching
    sys.modules["skills"].
    """
    global _skills_loader_cache
    if _skills_loader_cache is None:
        pkg_name = "_taskflow_real_skills"
        pkg_dir = _ROOT / "skills"
        if pkg_name not in sys.modules:
            pkg = stdlib_types.ModuleType(pkg_name)
            pkg.__path__ = [str(pkg_dir)]
            sys.modules[pkg_name] = pkg
        _skills_loader_cache = importlib.import_module(f"{pkg_name}.loader")
    return _skills_loader_cache


def _real_agent_tools() -> Any:
    global _agent_tools_cache
    if _agent_tools_cache is None:
        _agent_tools_cache = _load_module_from_file(
            "_taskflow_real_agent_tools",
            _ROOT / "agent" / "tools" / "__init__.py",
            is_package=True,
        )
    return _agent_tools_cache


def _fix_stub_run_async() -> None:
    """Make ``from pub_func import run_async`` work inside the stub regime.

    The tests/unit/subagent conftest stubs ``pub_func`` with a PEP 562
    ``__getattr__`` that resolves a missing name by importing the same-named
    submodule and returning its re-export. For ``run_async`` that import has
    a side effect: the import machinery binds the ``pub_func.run_async``
    SUBMODULE as the package attribute, shadowing the re-export before
    ``IMPORT_FROM`` reads it. Real modules loaded afterwards (mcp_plugin's
    ``from pub_func import run_async``) therefore get the module object and
    ``run_async(client.get_tools())`` explodes as ``TypeError: 'module'
    object is not callable``. Bind the real function (run_async.py imports
    only asyncio/threading, no heavy deps) onto the stub so normal attribute
    lookup wins. No-op when pub_func is real or already fixed.
    """
    stub = sys.modules.get("pub_func")
    if stub is None:
        return
    if callable(getattr(stub, "run_async", None)):
        return
    real = _load_module_from_file(
        "_taskflow_real_pub_func_run_async",
        _ROOT / "pub_func" / "run_async.py",
    )
    setattr(stub, "run_async", real.run_async)


@pytest.fixture()
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the taskflow store module at a tmp_path db and reset init state."""
    from agent.tools.taskflow.registry import store_sqlite

    db_path = tmp_path / "taskflow_registry.db"
    monkeypatch.setattr(store_sqlite, "_DB_DIR", tmp_path)
    monkeypatch.setattr(store_sqlite, "_DB_PATH", db_path)
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    # Fresh lock per test, mirroring the subagent reference: a contended
    # acquire BINDS an asyncio.Lock to the acquiring test's event loop, and
    # pytest-asyncio creates a new loop per test.
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)
    return db_path


@pytest.fixture()
def scan_skills_real() -> Callable[[], list[dict[str, Any]]]:
    """scan_skills(use_cache=False) from the REAL loader module.

    ``use_cache=False`` bypasses skills_snapshot.json (loader.py:82-89 reads
    the snapshot first when caching is on), so the scan always re-globs
    SKILLS_DIR and reflects the skills/taskflow/SKILL.md file on disk.
    """

    def _scan() -> list[dict[str, Any]]:
        return _real_skills_loader().scan_skills(use_cache=False)

    return _scan


@pytest.fixture()
def skill_visible_to_real() -> Callable[[dict[str, Any], str], bool]:
    """The real loader's visibility contract (loader.py:63-79)."""

    def _visible(skill: dict[str, Any], caller_scope: str) -> bool:
        return bool(_real_skills_loader()._skill_visible_to(skill, caller_scope))

    return _visible


@pytest.fixture()
def build_main_tools_real() -> Callable[[], list]:
    """Call build_main_tools, robust against the unit-test stub regime.

    In a solo run of tests/unit/taskflow the real agent.tools package is
    importable and the call goes through normally. When tests/unit/subagent
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
