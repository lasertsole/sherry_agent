"""Hermetic e2e: the eight LSP tools share lazily-started servers, then reap them."""

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


def test_all_tools_share_one_server_then_reap(
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
    rename = json.loads(tools["lsp_rename"]._run(target, 1, 5, "renamed"))
    diagnostics = json.loads(tools["lsp_diagnostics"]._run(target))
    formatted = json.loads(tools["lsp_format"]._run(target))
    status = json.loads(tools["lsp_status"]._run())

    assert definition["count"] == 1
    assert references["count"] == 2
    assert symbols["symbols"][0]["name"] == "fakesym"
    assert calls["calls"][0]["name"] == "callerfn"
    assert rename["count"] == 1 and rename["applied"] is False
    assert diagnostics["count"] == 1
    assert formatted["supported"] is True
    assert status["count"] == 10

    # A single server serves every tool for the same (language, cwd).
    stats = get_manager().stats()
    assert len(stats) == 1
    assert stats[0]["language"] == "python"
    pid = stats[0]["pid"]
    assert pid is not None

    get_manager()._clear_for_tests()
    assert get_manager().active_count() == 0
    _assert_reaped(pid)


def test_two_languages_start_two_lazy_servers(
    point_to_fake, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHERRY_LSP_ROOT", str(fixture_repo))
    point_to_fake("python")
    point_to_fake("bash")
    script = fixture_repo / "run.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    tools = {tool.name: tool for tool in build_lsp_tools("sess")}

    json.loads(tools["lsp_goto_definition"]._run(str(fixture_repo / "m.py"), 5, 12))
    json.loads(tools["lsp_diagnostics"]._run(str(script)))

    stats = get_manager().stats()
    assert {entry["language"] for entry in stats} == {"python", "bash"}
    pids = [entry["pid"] for entry in stats]
    assert all(pid is not None for pid in pids)

    get_manager()._clear_for_tests()
    for pid in pids:
        _assert_reaped(pid)
