"""Semantic code search — RESEARCHER-only embedding index over the symbol table.

Phase 3 of the code-intel plan: symbol-level chunks embedded through the existing
``models/embed_model`` wrapper, stored as SQLite BLOBs (the
``context_engine/embeddings`` pattern), and queried by cosine similarity with an
optional reranker pass. Every entry point is fail-open; see each module's
docstring.
"""

from .chunker import CodeChunk, SymbolRecord, build_chunk
from .indexer import EmbeddingIndexResult, SemanticIndexer
from .search import (
    SemanticCodeSearchTool,
    SemanticHit,
    SemanticSearch,
    SemanticSearchInput,
    SemanticSearchResult,
)

__all__ = [
    "CodeChunk",
    "EmbeddingIndexResult",
    "SemanticCodeSearchTool",
    "SemanticHit",
    "SemanticIndexer",
    "SemanticSearch",
    "SemanticSearchInput",
    "SemanticSearchResult",
    "SymbolRecord",
    "build_chunk",
]
