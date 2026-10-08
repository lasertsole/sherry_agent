"""Cosine semantic search over the code embedding index, plus the LangChain tool.

Pipeline: embed the query → rank every stored chunk by cosine similarity → hand
the top candidates to the existing reranker → return the top-K snippets with
path and line range. Both model calls are **fail-open**: an unavailable
embedding model or reranker degrades the result (an actionable ``degraded``
message, or reranker-free cosine ordering) instead of raising.

The index is self-healed on every call (mirroring
``context_engine/embeddings/search.py``): :meth:`SemanticSearch.search` refreshes
the incremental symbol+embedding index first, so a search after edits does not
return stale symbols. When nothing changed the refresh is a cheap no-op.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field, PrivateAttr

from config.features.agent_side import (
    CODE_INTEL,
    CODE_INTEL_SEMANTIC,
    CodeIntelConfig,
    CodeIntelSemanticConfig,
)

from .indexer import SemanticIndexer, _unpack

_LOAD_SQL = """
    SELECT e.symbol_id, e.embedding, e.dim, e.chunk_text,
           s.name, s.kind, s.file_path, s.line_start, s.line_end
    FROM code_embeddings e
    JOIN symbols s ON s.id = e.symbol_id
    ORDER BY s.file_path, s.line_start
"""

_DEGRADED_NO_INDEX = (
    "No code embeddings are indexed yet. Build the index first with "
    "`explore` (or retry semantic_code_search once the embedding model is "
    "available); fall back to `terminal` / `explore` for keyword search."
)
_EMPTY_QUERY_MESSAGE = "empty query — provide a concept, symbol name, or natural-language intent"


@dataclass(slots=True)
class SemanticHit:
    """One ranked code chunk."""

    symbol_id: int
    name: str
    kind: str
    file_path: str
    line_start: int
    line_end: int
    score: float
    fragment: str
    rerank_score: float | None = None


@dataclass(slots=True)
class SemanticSearchResult:
    """Result of :meth:`SemanticSearch.search`."""

    query: str
    hits: list[SemanticHit] = field(default_factory=list)
    reranked: bool = False
    indexed: int = 0
    truncated: bool = False
    degraded: str | None = None


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity in pure Python (no numpy dependency, bounded memory)."""
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for x, y in zip(left, right):
        dot += x * y
        left_norm += x * x
        right_norm += y * y
    denom = math.sqrt(left_norm) * math.sqrt(right_norm)
    return dot / denom if denom else 0.0


class SemanticSearch:
    """Read side of the semantic index; owns the self-healing build step."""

    def __init__(
        self,
        db_path: str | Path,
        config: CodeIntelConfig | None = None,
        semantic_config: CodeIntelSemanticConfig | None = None,
        *,
        indexer: SemanticIndexer | None = None,
        embed_model: object | None = None,
        reranker: object | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._config = config or CODE_INTEL
        self._semantic = semantic_config or CODE_INTEL_SEMANTIC
        self._embed_model = embed_model
        self._reranker = reranker
        self._indexer = indexer or SemanticIndexer(
            self._db_path, self._config, self._semantic, embed_model=embed_model
        )

    # -- model access ------------------------------------------------------

    def _get_embed_model(self) -> object:
        if self._embed_model is None:
            from models import build_embed_model

            self._embed_model = build_embed_model()
        return self._embed_model

    # -- public API --------------------------------------------------------

    def search(
        self, query: str, root: str | Path, top_k: int | None = None
    ) -> SemanticSearchResult:
        """Self-heal the index, then rank by cosine (+ reranker when available)."""
        if not query or not query.strip():
            return SemanticSearchResult(query=query, degraded=_EMPTY_QUERY_MESSAGE)

        limit = self._clamp_top_k(top_k)
        index_result = self._indexer.build(root)
        if (
            index_result.error is not None
            and index_result.embedded == 0
            and index_result.total == 0
        ):
            return SemanticSearchResult(query=query, degraded=index_result.error)

        conn = self._connect()
        try:
            rows = conn.execute(_LOAD_SQL).fetchall()
        finally:
            conn.close()

        if not rows:
            return SemanticSearchResult(
                query=query,
                degraded=index_result.error or _DEGRADED_NO_INDEX,
                truncated=index_result.truncated,
            )

        try:
            query_vector = self._get_embed_model().embed_query(query)  # type: ignore[attr-defined]
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - fail-open
            logger.warning("code_intel.semantic: query embedding failed: {}", exc)
            return SemanticSearchResult(
                query=query,
                indexed=len(rows),
                degraded=f"embedding model unavailable for the query ({exc}); no semantic ranking",
            )

        dims = {int(row["dim"]) for row in rows}
        if dims != {len(query_vector)}:
            return SemanticSearchResult(
                query=query,
                indexed=len(rows),
                degraded=(
                    f"embedding dimension mismatch: index holds {sorted(dims)} but the query "
                    f"is {len(query_vector)}-dimensional. Rebuild the index (delete it or change "
                    f"the model back) before searching."
                ),
            )

        scored: list[SemanticHit] = []
        for row in rows:
            vector = _unpack(row["embedding"])
            if len(vector) != len(query_vector):
                continue
            scored.append(
                SemanticHit(
                    symbol_id=int(row["symbol_id"]),
                    name=row["name"],
                    kind=row["kind"],
                    file_path=row["file_path"],
                    line_start=int(row["line_start"]),
                    line_end=int(row["line_end"]),
                    score=round(cosine(query_vector, vector), 6),
                    fragment=row["chunk_text"],
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.file_path, hit.line_start))
        pool = scored[: max(limit, int(self._semantic["code_intel_semantic_candidate_pool"]))]
        reranked_hits, reranked = self._rerank(query, pool)
        return SemanticSearchResult(
            query=query,
            hits=reranked_hits[:limit],
            reranked=reranked,
            indexed=len(rows),
            truncated=index_result.truncated,
        )

    # -- internals ---------------------------------------------------------

    def _clamp_top_k(self, top_k: int | None) -> int:
        default_k = max(1, int(self._semantic["code_intel_semantic_default_top_k"]))
        max_k = max(1, int(self._semantic["code_intel_semantic_max_top_k"]))
        requested = default_k if top_k is None else int(top_k)
        return max(1, min(requested, max_k))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _rerank(self, query: str, candidates: list[SemanticHit]) -> tuple[list[SemanticHit], bool]:
        """Rerank candidates with the existing reranker; fail-open to cosine order."""
        if len(candidates) <= 1:
            return candidates, False
        try:
            if self._reranker is None:
                from models import build_reranker_model

                self._reranker = build_reranker_model()
            ranked = self._reranker.rank(  # type: ignore[attr-defined]
                query, [hit.fragment for hit in candidates], top_k=len(candidates)
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - reranker is an optional refinement
            logger.warning(
                "code_intel.semantic: reranker unavailable, keeping cosine order: {}", exc
            )
            return candidates, False

        if not ranked:
            return candidates, False

        reordered: list[SemanticHit] = []
        used: set[int] = set()
        for entry in ranked:
            index = entry.get("corpus_id")
            if not isinstance(index, int) or not 0 <= index < len(candidates) or index in used:
                continue
            used.add(index)
            reordered.append(
                replace(candidates[index], rerank_score=float(entry.get("score", 0.0)))
            )
        if not reordered:
            return candidates, False
        reordered.extend(hit for index, hit in enumerate(candidates) if index not in used)
        return reordered, True


# ── LangChain tool ────────────────────────────────────────────────────────────


class SemanticSearchInput(BaseModel):
    """Input for the ``semantic_code_search`` tool."""

    query: str = Field(
        description="Concept, intent, or natural-language description of the code to find."
    )
    top_k: int | None = Field(
        default=None,
        description="Maximum number of snippets to return (defaults to the configured top-K).",
    )


class SemanticCodeSearchTool(BaseTool):
    """Concept/intent → ranked code snippets (RESEARCHER only)."""

    model_config = {"arbitrary_types_allowed": True}

    name: str = "semantic_code_search"
    description: str = (
        "Search code by concept or natural-language intent. Returns the most relevant code "
        "snippets ranked by semantic similarity. Use when explore (symbol name match) returns "
        "nothing, or when the query is a concept ('how does authentication work') rather than "
        "a symbol name. Examples: 'database connection pooling', 'error handling for "
        "websockets', 'session cleanup on disconnect'."
    )
    args_schema: type[BaseModel] = SemanticSearchInput
    metadata: dict[str, Any] = {"scope": "researcher_only", "idempotent": True}

    _engine: SemanticSearch = PrivateAttr()
    _root: Path = PrivateAttr()
    _session_id: str = PrivateAttr(default="")

    def __init__(
        self,
        engine: SemanticSearch,
        root: Path,
        session_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._engine = engine
        self._root = root
        self._session_id = session_id

    def _run(self, query: str, top_k: int | None = None) -> str:
        result = self._engine.search(query, self._root, top_k=top_k)
        return json.dumps(_payload(result), ensure_ascii=False)

    async def _arun(self, query: str, top_k: int | None = None) -> str:
        return await asyncio.to_thread(lambda: self._run(query, top_k))


def _payload(result: SemanticSearchResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": result.query,
        "count": len(result.hits),
        "indexed": result.indexed,
        "reranked": result.reranked,
        "truncated": result.truncated,
        "degraded": result.degraded,
        "results": [
            {
                "name": hit.name,
                "kind": hit.kind,
                "file": hit.file_path,
                "line_start": hit.line_start,
                "line_end": hit.line_end,
                "score": hit.score,
                "rerank_score": hit.rerank_score,
                "fragment": hit.fragment,
            }
            for hit in result.hits
        ],
    }
    return payload
