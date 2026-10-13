"""Unit tests for cosine semantic search + fail-open reranker handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.code_intel.semantic import SemanticIndexer, SemanticSearch
from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeIntelConfig, CodeIntelSemanticConfig

from .conftest import FakeEmbedding, FailingEmbedding, FailingReranker

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def semantic_config() -> CodeIntelSemanticConfig:
    return CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)


def _indexer(
    index_db: Path, intel_config: CodeIntelConfig, semantic_config, model
) -> SemanticIndexer:
    return SemanticIndexer(str(index_db), intel_config, semantic_config, embed_model=model)


def _search(
    index_db: Path,
    intel_config: CodeIntelConfig,
    semantic_config,
    *,
    embed_model=None,
    reranker=None,
    indexer=None,
) -> SemanticSearch:
    return SemanticSearch(
        str(index_db),
        intel_config,
        semantic_config,
        indexer=indexer,
        embed_model=embed_model,
        reranker=reranker,
    )


def _indexed_search(sample_repo, index_db, intel_config, semantic_config, *, reranker=None):
    engine = _search(
        index_db,
        intel_config,
        semantic_config,
        embed_model=FakeEmbedding(),
        reranker=reranker,
    )
    engine.search("warm the index", sample_repo)
    return engine


def test_cosine_ranking_surfaces_the_matching_symbol(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    engine = _indexed_search(sample_repo, index_db, intel_config, semantic_config)
    result = engine.search("top_func", sample_repo, top_k=5)

    assert result.degraded is None
    assert result.hits
    assert any(hit.name == "top_func" for hit in result.hits)
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True)


def test_top_k_is_clamped_to_the_configured_maximum(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    engine = _indexed_search(sample_repo, index_db, intel_config, semantic_config)

    one = engine.search("top_func", sample_repo, top_k=1)
    assert len(one.hits) == 1

    huge = engine.search("top_func", sample_repo, top_k=10_000)
    assert len(huge.hits) <= semantic_config["code_intel_semantic_max_top_k"]


def test_reranker_reorders_candidates(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config, fake_reranker
) -> None:
    engine = _indexed_search(
        sample_repo, index_db, intel_config, semantic_config, reranker=fake_reranker
    )
    result = engine.search("top_func", sample_repo, top_k=5)

    assert result.reranked is True
    assert fake_reranker.calls >= 1
    assert all(hit.rerank_score is not None for hit in result.hits)


def test_reranker_failure_falls_back_to_cosine(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    engine = _indexed_search(
        sample_repo, index_db, intel_config, semantic_config, reranker=FailingReranker()
    )
    result = engine.search("top_func", sample_repo, top_k=5)

    assert result.reranked is False
    assert result.hits
    assert all(hit.rerank_score is None for hit in result.hits)


def test_empty_query_degrades(
    index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    result = _search(index_db, intel_config, semantic_config, embed_model=FakeEmbedding()).search(
        "   ", Path(".")
    )
    assert result.degraded is not None
    assert result.hits == []


def test_empty_index_degrades_with_actionable_message(
    tmp_path: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    engine = _search(index_db, intel_config, semantic_config, embed_model=FakeEmbedding())
    result = engine.search("anything", empty)

    assert result.degraded is not None
    assert "terminal" in result.degraded or "explore" in result.degraded


def test_dimension_mismatch_degrades(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    indexer = _indexer(index_db, intel_config, semantic_config, FakeEmbedding(dim=8))
    indexer.build(sample_repo)

    engine = _search(
        index_db, intel_config, semantic_config, indexer=indexer, embed_model=FakeEmbedding(dim=16)
    )
    result = engine.search("top_func", sample_repo)

    assert result.degraded is not None
    assert "dimension mismatch" in result.degraded
    assert result.hits == []


def test_query_embedding_failure_degrades(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    indexer = _indexer(index_db, intel_config, semantic_config, FakeEmbedding())
    indexer.build(sample_repo)

    engine = _search(
        index_db, intel_config, semantic_config, indexer=indexer, embed_model=FailingEmbedding()
    )
    result = engine.search("top_func", sample_repo)

    assert result.degraded is not None
    assert "unavailable for the query" in result.degraded


def test_search_self_heals_after_edits(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig, semantic_config
) -> None:
    engine = _indexed_search(sample_repo, index_db, intel_config, semantic_config)
    assert not any(hit.name == "brand_new" for hit in engine.search("brand_new", sample_repo).hits)

    target = sample_repo / "pkg" / "main.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef brand_new(x):\n    return x * 3\n",
        encoding="utf-8",
    )

    refreshed = engine.search("brand_new", sample_repo, top_k=5)
    assert any(hit.name == "brand_new" for hit in refreshed.hits)


def test_truncated_index_is_reported(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    semantic_config = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    semantic_config["code_intel_semantic_max_chunks"] = 2
    engine = _search(index_db, intel_config, semantic_config, embed_model=FakeEmbedding())
    result = engine.search("top_func", sample_repo)

    assert result.truncated is True
