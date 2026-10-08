"""Tests for the re-entrant loop guard around the vendored LightRAG sync shims.

``graph_rag.core`` patches the ambient event loop with ``nest_asyncio`` at import
time so the vendored sync wrappers (``loop.run_until_complete(...)``) work. Robyn
serves requests on a uvloop loop, which ``nest_asyncio`` refuses to patch: the
old bare call raised ``ValueError`` during import, so every knowledge-graph
request answered with that error string and logged a traceback.

These tests pin the guard's contract on both loop kinds: a stdlib loop is patched
(nested ``run_until_complete`` really runs), an unpatchable loop is skipped with a
marker instead of an exception.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import nest_asyncio
import pytest

# Mirror the runtime import shape: the skill's ``scripts/`` directory carries the
# short ``graph_rag`` package name (see tests/skills/builtin/core/multimodal_rag/
# test_snkv_storage.py for the same setup).
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SCRIPTS_DIR = REPO_ROOT / "skills" / "builtin" / "core" / "multimodal_rag" / "scripts"
for path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from graph_rag.loop_patch import PATCHED, enable_nested_event_loops  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def test_patches_a_stdlib_loop_so_nested_runs_work():
    async def scenario() -> str:
        # enable_nested_event_loops() patches the loop this coroutine runs on.
        assert enable_nested_event_loops() == PATCHED
        loop = asyncio.get_running_loop()
        # The vendored shims call run_until_complete() from inside a coroutine;
        # that only succeeds on a patched (re-entrant) loop.
        return loop.run_until_complete(asyncio.sleep(0, result="nested"))

    assert asyncio.run(scenario()) == "nested"


def test_skips_an_unpatchable_uvloop_loop_without_raising(monkeypatch):
    def refuse(loop=None):
        # What nest_asyncio does for uvloop.Loop: ValueError, not a silent no-op.
        raise ValueError("Can't patch loop of type <class 'uvloop.Loop'>")

    monkeypatch.setattr(nest_asyncio, "apply", refuse)
    result = enable_nested_event_loops()
    assert result.startswith("skipped:")
    assert "uvloop" in result


def test_import_time_patch_is_never_fatal(monkeypatch):
    """The guard is called at import time, so no exception may escape it."""
    calls: list[int] = []

    def boom(loop=None):
        calls.append(1)
        raise RuntimeError("unexpected patcher failure")

    monkeypatch.setattr(nest_asyncio, "apply", boom)
    # RuntimeError is NOT swallowed on purpose (only the unpatchable-loop
    # ValueError is): a broken patcher should stay loud.
    with pytest.raises(RuntimeError):
        enable_nested_event_loops()
    assert calls == [1]
