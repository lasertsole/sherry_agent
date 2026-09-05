"""Sweeper startup wiring tests (Task 5, loop-detection-cron-breaker).

ISOLATION CHOICE (documented per task spec): ``server/trigger/channels/core.py``
must NOT be imported wholesale in a test process -- its module top-level has
real side effects (starts the channel manager thread, boots the QQ channel and
the heartbeat service). Therefore the ``_schedule_sweeper`` function is
extracted from the PRODUCTION SOURCE via AST and executed in an isolated
namespace (same isolation principle as the guardrail tests'
``spec_from_file_location`` rule, but stricter: only the target function body
runs). The helper imports ``start_sweeper`` lazily at call time, so a stub
module installed in ``sys.modules`` exercises it without loading the heavy
subagent registry import chain.
"""

import ast
import asyncio
import concurrent.futures
import sys
import threading
import types
from pathlib import Path

from loguru import logger

CORE_PATH = (
    Path(__file__).resolve().parents[3] / "server" / "trigger" / "channels" / "core.py"
)
REGISTRY_MODULE = "agent.tools.subagent.registry"


def _load_schedule_sweeper():
    """Compile the real ``_schedule_sweeper`` out of core.py's source."""
    source = CORE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "_schedule_sweeper"
        ):
            module = ast.Module(body=[node], type_ignores=[])
            code = compile(module, str(CORE_PATH), "exec")
            ns: dict[str, object] = {
                "asyncio": asyncio,
                "concurrent": concurrent,
                "logger": logger,
            }
            exec(code, ns)  # noqa: S102 - trusted repo source, isolated namespace
            func = ns["_schedule_sweeper"]
            assert callable(func), "AST extraction did not yield a callable"
            return func
    raise AssertionError(
        "_schedule_sweeper not found in server/trigger/channels/core.py"
    )


def _install_registry_stub(monkeypatch):
    """Stub ``agent.tools.subagent.registry`` so the lazy import resolves to
    a recording fake instead of the heavy real registry."""
    stub = types.ModuleType(REGISTRY_MODULE)
    awaited = []

    async def _fake_start_sweeper():
        awaited.append(True)
        await asyncio.sleep(0)

    setattr(stub, "start_sweeper", _fake_start_sweeper)
    setattr(stub, "stop_sweeper", lambda: None)
    monkeypatch.setitem(sys.modules, REGISTRY_MODULE, stub)
    return awaited


def test_schedule_sweeper_schedules_start_sweeper_on_running_loop(monkeypatch):
    awaited = _install_registry_stub(monkeypatch)
    schedule_sweeper = _load_schedule_sweeper()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        future = schedule_sweeper(loop)

        assert isinstance(future, concurrent.futures.Future), (
            "expected a Future from a running loop"
        )
        future.result(timeout=5)  # blocks until start_sweeper() finished
        assert awaited == [True], "start_sweeper stub was not awaited exactly once"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_schedule_sweeper_returns_none_on_closed_loop(monkeypatch):
    _install_registry_stub(monkeypatch)
    schedule_sweeper = _load_schedule_sweeper()

    loop = asyncio.new_event_loop()
    loop.close()

    # Must not raise even though the loop is already closed.
    result = schedule_sweeper(loop)
    assert result is None, "expected None when the event loop is closed"
