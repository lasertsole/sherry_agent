"""Real-embedding smoke test (opt-in by backend availability).

Builds the embedding index over the small 4-language fixture repo with the REAL
configured embedding backend (``models.build_embed_model`` — local bge-m3 GGUF
by default, or a configured remote API) and asserts a natural-language query
returns a relevant code fragment.

The test SKIPS (never fails, never downloads) when the backend cannot run in
this environment:

* local mode requires both ``llama-cpp-python`` and the already-present GGUF
  weights — CI checkouts have neither, so the local path skips there instead of
  pulling 634 MB;
* remote mode requires a non-empty ``EMBEDDING_API_BASE`` / ``EMBEDDING_API_KEY``.

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
    """Return a skip reason when the configured real embedding backend cannot run."""
    try:
        from models.embed_model import core as embed_model_core
        from models.utils import read_env_file_value as read_env

        _, use_local, _ = embed_model_core._detect_backend()
    except SystemExit:  # misconfigured remote mode aborts at detection
        return "embedding backend misconfigured (EMBEDDING_* validation aborted)"

    if not use_local:
        if (
            not read_env("EMBEDDING_API_BASE", "").strip()
            or not read_env("EMBEDDING_API_KEY", "").strip()
        ):
            return "remote embedding backend unconfigured (EMBEDDING_API_BASE/KEY empty)"
        return None

    try:
        from llama_cpp import Llama
    except ModuleNotFoundError:
        return "llama-cpp-python is not installed for the local bge-m3 backend"

    if not hasattr(Llama, "from_pretrained"):
        return "importable llama_cpp is a test stub without from_pretrained"

    weights = Path(embed_model_core._GGUF_MODEL_PATH)
    if not weights.is_file():
        return f"local bge-m3 GGUF weights absent ({weights}); tests never download weights"
    return None


def test_real_embedding_build_then_concept_search(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    """Given the real embedding backend and the small fixture repo,
    when the semantic index is built and a concept query is issued,
    then at least one semantically relevant fixture symbol is returned.
    """
    reason = _embedding_backend_unavailable()
    if reason is not None:
        pytest.skip(reason)

    from models import build_embed_model

    before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    semantic_config = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    engine = SemanticSearch(
        str(index_db),
        intel_config,
        semantic_config,
        embed_model=build_embed_model(),
        reranker=None,  # resolved lazily; an unavailable reranker degrades, never fails
    )

    result = engine.search(
        "a helper function that increments a numeric value", sample_repo, top_k=5
    )
    after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        f"\n[semantic smoke] indexed={result.indexed} hits={len(result.hits)} "
        f"reranked={result.reranked} degraded={result.degraded!r} "
        f"maxrss_before={before_kb // 1024}MB maxrss_after={after_kb // 1024}MB"
    )

    assert result.degraded is None, f"semantic search degraded: {result.degraded}"
    assert result.hits, "no semantic hits returned for the concept query"
    names = {hit.name for hit in result.hits}
    assert "helper" in names or "top_func" in names, f"unexpected symbols: {names}"
    for hit in result.hits:
        assert hit.file_path and hit.line_start >= 1 and hit.fragment
