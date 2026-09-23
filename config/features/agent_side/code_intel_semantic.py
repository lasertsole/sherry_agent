"""Semantic code search configuration (embedding index over the symbol table).

A single ``TypedDict`` + a single default instance, mirroring every other
per-object feature module. The embedding backend is deliberately NOT configured
here: Phase 3 reuses the existing ``models/embed_model`` wrapper
(:func:`models.embed_model.core.build_embed_model`) selected by the existing
``EMBEDDING_*`` environment variables. Only bounded, resource-shaping knobs live
in this object — the host has a small memory budget, so batching and caps are
configuration, not constants.
"""

from typing import TypedDict


class CodeIntelSemanticConfig(TypedDict):
    """Bounded parameters for the embedding-backed semantic code search."""

    # Model name recorded in the ``code_embeddings.model`` column. A stored row
    # with a different model invalidates the index (rebuilt, never mixed).
    code_intel_semantic_model: str
    # Default / maximum number of hits returned to the model.
    code_intel_semantic_default_top_k: int
    code_intel_semantic_max_top_k: int
    # Cosine-ranked candidates handed to the reranker before the final top-K.
    code_intel_semantic_candidate_pool: int
    # Chunks embedded per ``embed_documents`` call (each local call reloads the
    # GGUF, so batches trade load time against peak memory).
    code_intel_semantic_batch_size: int
    # Hard ceiling on chunks embedded per build run (bounds memory and wall time).
    code_intel_semantic_max_chunks: int
    # Hard ceiling on chunks contributed by a single file.
    code_intel_semantic_max_chunks_per_file: int
    # Per-chunk character ceiling (header + symbol body).
    code_intel_semantic_max_chunk_chars: int


CODE_INTEL_SEMANTIC: CodeIntelSemanticConfig = {
    "code_intel_semantic_model": "bge-m3",
    "code_intel_semantic_default_top_k": 5,
    "code_intel_semantic_max_top_k": 20,
    "code_intel_semantic_candidate_pool": 40,
    "code_intel_semantic_batch_size": 16,
    "code_intel_semantic_max_chunks": 1000,
    "code_intel_semantic_max_chunks_per_file": 60,
    "code_intel_semantic_max_chunk_chars": 2000,
}
