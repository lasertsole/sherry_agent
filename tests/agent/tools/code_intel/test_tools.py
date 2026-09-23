"""Unit tests for the four LangChain code_intel tool wrappers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.tools.code_intel import build_code_intel_tools
from config.features.agent_side import CodeIntelConfig

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def tools(sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig) -> list:
    return build_code_intel_tools(
        "sess-1", config=intel_config, root=sample_repo, db_path=str(index_db)
    )


def test_tool_names_and_scope_metadata(tools: list) -> None:
    assert [tool.name for tool in tools] == [
        "explore",
        "callers",
        "callees",
        "impact",
        "semantic_code_search",
    ]
    for tool in tools:
        assert tool.metadata["scope"] == "researcher_only"
        assert tool.description


def test_session_id_injected(tools: list) -> None:
    for tool in tools:
        assert tool._session_id == "sess-1"


def test_explore_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[0]._run(query="top_func"))
    assert payload["count"] > 0
    assert any(result["name"] == "top_func" for result in payload["results"])


def test_callers_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[1]._run(symbol="helper"))
    assert payload["count"] > 0
    assert any(caller["name"] == "top_func" for caller in payload["callers"])


def test_callees_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[2]._run(symbol="top_func"))
    assert any(callee["name"] == "helper" for callee in payload["callees"])


def test_impact_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[3]._run(symbol="helper"))
    assert payload["count"] > 0
    assert any(entry["name"] in {"top_func", "TopFunc"} for entry in payload["affected"])


def test_unknown_symbol_is_empty_not_error(tools: list) -> None:
    payload = json.loads(tools[1]._run(symbol="no_such_symbol_abc"))
    assert payload["count"] == 0
    assert payload["callers"] == []


def test_async_arun_returns_json(tools: list) -> None:
    payload = json.loads(asyncio.run(tools[0]._arun(query="top_func")))
    assert payload["count"] > 0


def test_args_schemas(tools: list) -> None:
    assert "query" in tools[0].args_schema.model_fields
    assert "symbol" in tools[1].args_schema.model_fields
    assert "symbol" in tools[2].args_schema.model_fields
    assert "symbol" in tools[3].args_schema.model_fields
    assert set(tools[4].args_schema.model_fields) == {"query", "top_k"}
