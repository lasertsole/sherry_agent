"""Real-embedding smoke test for the code-specific embedder (opt-in).

Builds the embedding index over the small 4-language fixture repo with the REAL
Jina v2 Base Code ONNX backend and asserts a natural-language query returns a
relevant code fragment.

The test SKIPS (never fails, never downloads) when the backend cannot run in
this environment: onnxruntime/transformers missing, or the ONNX/tokenizer files
absent. CI checkouts do not carry the 612 MB weights.

The reranker is optional: an unavailable reranker must degrade to cosine order
(fail-open), which this test records rather than asserts.
"""

from __future__ import annotations

import resource
from pathlib import Path

import pytest

from agent.tools.code_intel.semantic import SemanticSearch
from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeIntelConfig, CodeIntelSemanticConfig

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


def _embedding_backend_unavailable() -> str | None:
    """Return a skip reason when the real code embedder cannot run here."""
    try:
        import onnxruntime  # noqa: F401
        from transformers import RobertaTokenizerFast  # noqa: F401
    except ModuleNotFoundError as exc:
        return f"code embedder runtime missing: {exc.name}"

    from models.embed_model.code import code_embedder_available

    if not code_embedder_available():
        return "Jina v2 Base Code ONNX/tokenizer files absent; tests never download weights"
    return None


def test_real_embedding_build_then_concept_search(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    """Given the real code embedder and the small fixture repo,
    when the semantic index is built and a concept query is issued,
    then at least one semantically relevant fixture symbol is returned.
    """
    reason = _embedding_backend_unavailable()
    if reason is not None:
        pytest.skip(reason)

    from models.embed_model.code import build_code_embed_model

    before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    semantic_config = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    engine = SemanticSearch(
        str(index_db),
        intel_config,
        semantic_config,
        embed_model=build_code_embed_model(),
        reranker=None,  # resolved lazily; an unavailable reranker degrades, never fails
    )

    result = engine.search(
        "a helper function that increments a numeric value", sample_repo, top_k=5
    )
    after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        f"\n[semantic smoke] indexed={result.indexed} hits={len(result.hits)} "
        f"reranked={result.reranked} degraded={result.degraded!r} "
        f"embedder={result.embedder!r} "
        f"maxrss_before={before_kb // 1024}MB maxrss_after={after_kb // 1024}MB"
    )

    assert result.degraded is None, f"semantic search degraded: {result.degraded}"
    assert result.hits, "no semantic hits returned for the concept query"
    names = {hit.name for hit in result.hits}
    assert "helper" in names or "top_func" in names, f"unexpected symbols: {names}"
    for hit in result.hits:
        assert hit.file_path and hit.line_start >= 1 and hit.fragment
