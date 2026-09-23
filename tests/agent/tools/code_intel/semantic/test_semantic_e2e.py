"""Hermetic end-to-end: build the embedding index, then search through the tool.

Covers build → search through the real ``semantic_code_search`` LangChain tool
with a deterministic fake embedding backend and a fake reranker, plus the
tool-level fail-open path when the backend is down. No model, disk weights or
network are involved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.tools.code_intel import build_code_intel_tools
from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeIntelConfig, CodeIntelSemanticConfig

from .conftest import FakeEmbedding, FailingEmbedding, PassthroughReranker

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]


def _tools(sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, **kwargs) -> list:
    return build_code_intel_tools(
        "e2e-semantic",
        config=intel_config,
        semantic_config=kwargs.pop(
            "semantic_config", CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
        ),
        root=sample_repo,
        db_path=str(index_db),
        embed_model=kwargs.pop("embed_model", FakeEmbedding()),
        reranker=kwargs.pop("reranker", PassthroughReranker()),
    )


def test_tool_surface_has_five_researcher_tools(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tools = _tools(sample_repo, index_db, intel_config)
    assert [tool.name for tool in tools] == [
        "explore",
        "callers",
        "callees",
        "impact",
        "semantic_code_search",
    ]
    for tool in tools:
        assert tool.metadata["scope"] == "researcher_only"


def test_semantic_tool_has_no_path_input(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tool = _tools(sample_repo, index_db, intel_config)[4]
    assert set(tool.args_schema.model_fields) == {"query", "top_k"}


def test_build_then_search_returns_relevant_fragment(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tool = _tools(sample_repo, index_db, intel_config)[4]

    payload = json.loads(tool._run(query="top_func", top_k=5))
    assert payload["count"] >= 1
    assert payload["degraded"] is None
    assert payload["reranked"] is True
    hits = [result for result in payload["results"] if result["name"] == "top_func"]
    assert hits
    for hit in hits:
        assert hit["file"]
        assert hit["line_start"] >= 1
        assert hit["fragment"]


def test_semantic_tool_arun_returns_json(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tool = _tools(sample_repo, index_db, intel_config)[4]
    payload = json.loads(asyncio.run(tool._arun(query="top_func")))
    assert payload["count"] >= 1


def test_semantic_tool_degrades_when_backend_is_down(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tool = _tools(sample_repo, index_db, intel_config, embed_model=FailingEmbedding())[4]

    payload = json.loads(tool._run(query="top_func"))
    assert payload["count"] == 0
    assert payload["degraded"]
    assert payload["results"] == []
