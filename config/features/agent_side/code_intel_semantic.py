"""Semantic code search configuration (embedding index over the symbol table).

One ``TypedDict`` + one default instance, mirroring every other per-object
feature module. The code-intel semantic layer is **code-embedder-only**: BGE is
reserved for text semantics (``context_engine`` / multimodal RAG) and is never
used here. A profile table describes the accepted code embedder(s); only
resource-shaping knobs and the profile descriptors live in this object.
"""

from typing import TypedDict


class CodeEmbedderProfile(TypedDict):
    """One code-specific embedder the semantic layer may load."""

    name: str
    repo_id: str
    revision: str
    # Repo-relative ONNX file, e.g. "onnx/model.onnx"; downloaded into code/model_weight.
    onnx_filename: str
    # Tokenizer files downloaded into code/tokenizer; loaded with RobertaTokenizerFast.
    tokenizer_files: tuple[str, ...]
    dim: int
    pooling: str  # "mean" for Jina v2 Base Code
    max_length: int
    license: str
    natural_languages: tuple[str, ...]
    size_bytes: int


class CodeIntelSemanticConfig(TypedDict):
    """Bounded parameters for the embedding-backed semantic code search."""

    # Active CodeEmbedder profile. "bge-m3" is NOT a legal value.
    code_intel_semantic_model: str
    # off = no tool/model/index; on = require the profile (may download lazily);
    # auto = enable only when the profile's files are already present and valid.
    code_intel_semantic_enabled: str
    # Only code (or auto) is accepted; there is deliberately no bge-m3 option.
    code_intel_semantic_embedder: str
    code_intel_semantic_profiles: dict[str, CodeEmbedderProfile]
    # Default / maximum number of hits returned to the model.
    code_intel_semantic_default_top_k: int
    code_intel_semantic_max_top_k: int
    # Cosine-ranked candidates handed to the reranker before the final top-K.
    code_intel_semantic_candidate_pool: int
    # Chunks embedded per ``embed_documents`` call.
    code_intel_semantic_batch_size: int
    # Hard ceiling on chunks embedded per build run (bounds memory and wall time).
    code_intel_semantic_max_chunks: int
    # Hard ceiling on chunks contributed by a single file.
    code_intel_semantic_max_chunks_per_file: int
    # Per-chunk character ceiling (header + symbol body).
    code_intel_semantic_max_chunk_chars: int
    # Model download transport (the Model Config progress bar polls the byte count).
    code_intel_semantic_download_timeout_seconds: int
    code_intel_semantic_download_chunk_bytes: int


CODE_INTEL_SEMANTIC: CodeIntelSemanticConfig = {
    "code_intel_semantic_model": "jina-embeddings-v2-base-code",
    "code_intel_semantic_enabled": "auto",
    "code_intel_semantic_embedder": "code",
    "code_intel_semantic_profiles": {
        "jina-embeddings-v2-base-code": {
            "name": "jina-embeddings-v2-base-code",
            "repo_id": "jinaai/jina-embeddings-v2-base-code",
            "revision": "516f4baf13dec4ddddda8631e019b5737c8bc250",
            "onnx_filename": "onnx/model.onnx",
            "tokenizer_files": (
                "tokenizer.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
                "vocab.json",
            ),
            "dim": 768,
            "pooling": "mean",
            "max_length": 1024,
            "license": "Apache-2.0",
            "natural_languages": ("en", "zh"),
            "size_bytes": 641517466,
        }
    },
    "code_intel_semantic_default_top_k": 5,
    "code_intel_semantic_max_top_k": 20,
    "code_intel_semantic_candidate_pool": 40,
    "code_intel_semantic_batch_size": 16,
    "code_intel_semantic_max_chunks": 1000,
    "code_intel_semantic_max_chunks_per_file": 60,
    "code_intel_semantic_max_chunk_chars": 2000,
    "code_intel_semantic_download_timeout_seconds": 900,
    "code_intel_semantic_download_chunk_bytes": 1_048_576,
}
