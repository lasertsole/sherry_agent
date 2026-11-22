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
