"""Eval sandbox child tool policy: full by default, opt-in read-only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.sandbox import SAFE_TOOL_NAMES, EvalSandbox

pytestmark = [pytest.mark.unit]


def test_child_tools_default_to_the_full_builder(tmp_path: Path) -> None:
    import agent.tools

    original = agent.tools.build_main_tools
    sandbox = EvalSandbox(tmp_path)
    sandbox._restrict_child_tools()
    assert sandbox.restrict_child_tools is False
    assert agent.tools.build_main_tools is original


def test_opt_in_child_isolation_filters_to_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.tools

    fake_tools = [
        SimpleNamespace(name=name)
        for name in ("read_file", "write_file", "search_files", "question", "terminal")
    ]
    monkeypatch.setattr(agent.tools, "build_main_tools", lambda: list(fake_tools))
    sandbox = EvalSandbox(tmp_path, restrict_child_tools=True)
    sandbox._restrict_child_tools()
    try:
        names = {tool.name for tool in agent.tools.build_main_tools()}
    finally:
        sandbox.restore()
    assert names == set(SAFE_TOOL_NAMES)


def test_restore_restores_real_registry_paths(tmp_path: Path) -> None:
    """Repeated redirects of a shared module must not poison the snapshot."""
    from config.path import resolve_registry_db_path

    sandbox = EvalSandbox(tmp_path)
    sandbox.apply()
    assert sandbox._applied
    sandbox.restore()
    from agent.tools.subagent.registry import pending_injections

    assert Path(pending_injections._DB_PATH) == resolve_registry_db_path()


def test_restore_undoes_iteration_budget_override(tmp_path: Path) -> None:
    """Two suites in one process must not inherit each other's budget pin."""
    from config.features import ITERATION_BUDGET

    from evals.shared.eval_iteration_budget import pin_eval_budget, restore_eval_budget

    restore_eval_budget()
    original = int(ITERATION_BUDGET["main_agent_max_iterations"])
    sandbox = EvalSandbox(tmp_path)
    try:
        pin_eval_budget(999)
        assert ITERATION_BUDGET["main_agent_max_iterations"] == 999
    finally:
        sandbox.restore()
    assert ITERATION_BUDGET["main_agent_max_iterations"] == original


def test_restore_undoes_child_run_timeout_override(tmp_path: Path) -> None:
    """deepswe raises the process-wide spawn timeout; restore must undo it."""
    from agent.tools.subagent.config import get_config

    sandbox = EvalSandbox(tmp_path)
    sandbox.apply()
    original = get_config().run_timeout_seconds
    try:
        get_config().run_timeout_seconds = original + 123
        assert get_config().run_timeout_seconds == original + 123
    finally:
        sandbox.restore()
    assert get_config().run_timeout_seconds == original


def test_apply_does_not_noop_the_shared_memory_backflow(tmp_path: Path) -> None:
    """Backflow reloads the (redirected) memory DB; no-op'ing hid child writes."""
    import importlib

    drain = importlib.import_module("agent.middlewares.subagent_completion_drain.core")
    real_backflow = drain._backflow_shared_memory
    sandbox = EvalSandbox(tmp_path)
    try:
        sandbox.apply()
        # The production function is left in place (only the DB path is redirected).
        assert drain._backflow_shared_memory is real_backflow
        assert callable(drain._backflow_shared_memory)
    finally:
        sandbox.restore()


@pytest.mark.asyncio
async def test_close_aiosqlite_connections_inside_a_running_loop(tmp_path: Path) -> None:
    """Async suites must still close connections (and their worker threads)."""
    import aiosqlite

    conn = await aiosqlite.connect(tmp_path / "sandbox.db")
    sandbox = EvalSandbox.__new__(EvalSandbox)
    sandbox._opened_connections = [conn]

    assert sandbox.close_aiosqlite_connections() == 1
    assert conn._connection is None
    assert sandbox._opened_connections == []
