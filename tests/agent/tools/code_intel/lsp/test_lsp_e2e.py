"""Hermetic e2e: the four LSP tools share one lazily-started server, then reap it."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp import build_lsp_tools
from agent.tools.code_intel.lsp.manager import get_manager

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


def _assert_reaped(pid: int | None) -> None:
    assert pid is not None
    if sys.platform == "win32":
        return
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_tools_share_one_server_then_reap(
    point_to_fake, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHERRY_LSP_ROOT", str(fixture_repo))
    point_to_fake("python")
    tools = {tool.name: tool for tool in build_lsp_tools("sess")}
    target = str(fixture_repo / "m.py")

    definition = json.loads(tools["lsp_goto_definition"]._run(target, 5, 12))
    references = json.loads(tools["lsp_find_references"]._run(target, 1, 5))
    symbols = json.loads(tools["lsp_workspace_symbol"]._run("fake", file_path=target))
    calls = json.loads(tools["lsp_call_hierarchy"]._run(target, 1, 5, "incoming"))

    assert definition["count"] == 1
    assert references["count"] == 2
    assert symbols["symbols"][0]["name"] == "fakesym"
    assert calls["calls"][0]["name"] == "callerfn"

    # A single server serves all four calls for the same (language, cwd).
    stats = get_manager().stats()
    assert len(stats) == 1
    assert stats[0]["language"] == "python"
    pid = stats[0]["pid"]
    assert pid is not None

    get_manager()._clear_for_tests()
    assert get_manager().active_count() == 0
    _assert_reaped(pid)
