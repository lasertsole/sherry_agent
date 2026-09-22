"""Hermetic end-to-end: index a 4-language fixture repo, then query via the tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.code_intel import CodeIndexer, build_code_intel_tools
from config.features.agent_side import CodeIntelConfig

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]


def test_fixture_repo_explore_callers_callees_impact(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, db_rows
) -> None:
    tools = build_code_intel_tools(
        "e2e", config=intel_config, root=sample_repo, db_path=str(index_db)
    )
    explore, callers_tool, callees_tool, impact_tool = tools

    explore_payload = json.loads(explore._run(query="top_func"))
    assert explore_payload["count"] >= 1
    assert any(
        result["name"] == "top_func" and result["file"] == "pkg/main.py"
        for result in explore_payload["results"]
    )

    callers_payload = json.loads(callers_tool._run(symbol="helper"))
    assert any(caller["name"] == "top_func" for caller in callers_payload["callers"])

    callees_payload = json.loads(callees_tool._run(symbol="top_func"))
    assert any(
        callee["name"] == "helper" and callee["resolved"] for callee in callees_payload["callees"]
    )

    impact_payload = json.loads(impact_tool._run(symbol="helper"))
    assert impact_payload["count"] >= 1

    languages = {row["language"] for row in db_rows("SELECT DISTINCT language FROM symbols")}
    assert {"python", "typescript", "rust", "go"} <= languages


def test_index_db_is_reused_without_rebuild(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tools = build_code_intel_tools(
        "e2e", config=intel_config, root=sample_repo, db_path=str(index_db)
    )
    assert tools[0]._run(query="top_func")
    assert index_db.exists()
    before = index_db.stat().st_mtime_ns

    indexer = CodeIndexer(str(index_db), intel_config)
    result = indexer.index_directory(sample_repo)
    assert result.indexed_files == 0
    assert result.skipped_files > 0
    assert index_db.stat().st_mtime_ns == before
