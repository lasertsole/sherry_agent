"""Semantic recall over long-term memory entries.

The complete pipeline, in order:

1. self-heal the vector index (``index_pending_memory_entries``);
2. embed the query and take the cosine top-N (``N = limit * 3``);
3. re-rank that short list with the project's cross-encoder (bge-reranker);
4. apply the per-call character budget and the compaction-staleness check;
5. count a use for every entry actually returned (the usage feedback loop).

Everything is fail-open: a missing embedder, a broken reranker or an unreadable
epoch degrades to the next cheaper signal rather than raising into a model call.
The synchronous implementation exists because the ``memory`` tool's sync path
runs in a worker thread; the async wrapper is what the reminder middleware
awaits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from context_engine.store.memory_embeddings import (
    index_pending_memory_entries,
    load_memory_embeddings,
)
from context_engine.store.memory_entry_repository import (
    MemoryEntryRepository,
    memory_entry_repo,
)

__all__ = ["recall_memories", "semantic_recall"]

logger = logging.getLogger(__name__)


def _current_epoch(session_id: str | None) -> tuple[int, int]:
    """The session's ``(baseline_seq, updated_at_ms)``, or ``(0, 0)``.

    Read through the prompt-data provider so this context-engine module never
    imports the runtime state register. A missing session/provider means the
    epoch is unknown and no entry is considered stale.
    """
    if not session_id:
        return 0, 0
    from runtime import data_provider

    provider = data_provider.get_prompt_data_provider()
    getter = getattr(provider, "get_context_epoch", None) if provider is not None else None
    if getter is None:
        return 0, 0
    try:
        epoch, updated_ms = getter(session_id)
        return int(epoch), int(updated_ms)
    except Exception:  # noqa: BLE001 - staleness is advisory, never fatal
        logger.debug("memory recall: context epoch unavailable for %s", session_id, exc_info=True)
        return 0, 0


def _is_stale(entry: dict[str, Any], current_epoch: int, epoch_updated_ms: int) -> bool:
    """Whether an entry predates the session's latest context epoch.

    Entries created before the stamp existed (``compaction_epoch == 0``) have
    no epoch and are never treated as stale — they are old, not invalid.
    """
    entry_epoch = int(entry.get("compaction_epoch") or 0)
    if entry_epoch <= 0 or current_epoch <= 0 or entry_epoch >= current_epoch:
        return False
    last_touch = max(
        int(entry.get("last_used") or 0),
        int(entry.get("updated_at") or 0),
    )
    return last_touch < epoch_updated_ms


def _cosine_rank(
    query_vector: np.ndarray, candidates: list[dict[str, Any]], *, top_n: int
) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    query_norm = float(np.linalg.norm(query_vector))
    for candidate in candidates:
        vector = np.asarray(candidate.get("embedding") or [], dtype=np.float32)
        if vector.size == 0:
            continue
        norm = float(query_norm * np.linalg.norm(vector))
        if not norm:
            continue
        scored.append((float(np.dot(query_vector, vector) / norm), candidate))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{**candidate, "score": round(score, 4)} for score, candidate in scored[:top_n]]


def _rerank(query: str, candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Cross-encoder re-rank; a missing/failed backend keeps cosine order."""
    if not candidates:
        return []
    try:
        from models.reranker_model import build_reranker_model

        ranked = build_reranker_model().rank(
            query,
            [str(candidate["content"]) for candidate in candidates],
            top_k=limit,
        )
    except Exception:  # noqa: BLE001 - recall must survive a missing local model
        logger.debug("memory recall: reranker unavailable; keeping cosine order", exc_info=True)
        return candidates

    result: list[dict[str, Any]] = []
    for item in ranked:
        index = int(item.get("corpus_id", -1))
        if 0 <= index < len(candidates):
            result.append({**candidates[index], "score": float(item.get("score", 0.0))})
    return result or candidates


def recall_memories(
    query: str,
    target: str | None = None,
    limit: int = 5,
    char_budget: int = 4000,
    *,
    session_id: str | None = None,
    include_stale: bool = False,
    repo: MemoryEntryRepository | None = None,
) -> list[dict[str, Any]]:
    """Return memory entries relevant to *query*, ordered by relevance.

    Returns an empty list for a blank query, an empty store, or an unavailable
    embedder. ``char_budget`` caps the total returned content; an entry that
    would cross the cap is truncated (then the loop stops).
    """
    if not query or not query.strip():
        return []
    safe_limit = max(1, min(int(limit), 50))
    safe_budget = max(0, int(char_budget))
    if safe_budget == 0:
        return []

    repository = repo or memory_entry_repo
    try:
        index_pending_memory_entries(repository)
    except Exception:  # noqa: BLE001 - index self-heal is best-effort
        logger.debug("memory recall: index pass failed", exc_info=True)

    try:
        candidates = load_memory_embeddings(target, repository)
    except Exception:  # noqa: BLE001
        logger.exception("memory recall: could not load embeddings")
        return []
    if not candidates:
        return []

    try:
        from context_engine.embeddings.indexer import _get_embed_fn

        query_vector = np.asarray(_get_embed_fn()([query])[0], dtype=np.float32)
    except Exception:  # noqa: BLE001
        logger.debug("memory recall: query embedding failed", exc_info=True)
        return []

    shortlist = _cosine_rank(query_vector, candidates, top_n=safe_limit * 3)
    ranked = _rerank(query, shortlist, limit=safe_limit)

    current_epoch, epoch_updated_ms = _current_epoch(session_id)
    results: list[dict[str, Any]] = []
    remaining = safe_budget
    used_ids: list[int] = []
    for candidate in ranked[:safe_limit]:
        stale = _is_stale(candidate, current_epoch, epoch_updated_ms)
        if stale and not include_stale:
            continue
        content = str(candidate.get("content") or "")
        if len(content) > remaining:
            if remaining < 80:
                break
            content = content[:remaining]
        results.append(
            {
                "id": int(candidate["id"]),
                "target": candidate.get("target"),
                "content": content,
                "entry_type": candidate.get("entry_type"),
                "score": float(candidate.get("score") or 0.0),
                "usage_count": int(candidate.get("usage_count") or 0),
                "created_at": candidate.get("created_at"),
                "stale": stale,
            }
        )
        remaining -= len(content)
        used_ids.append(int(candidate["id"]))
        if remaining <= 0:
            break

    for entry_id in used_ids:
        try:
            repository.increment_usage(entry_id)
        except Exception:  # noqa: BLE001 - a usage miss is not a recall failure
            logger.debug("memory recall: usage increment failed for %s", entry_id, exc_info=True)
    return results


async def semantic_recall(
    query: str,
    target: str | None = None,
    limit: int = 5,
    char_budget: int = 4000,
    *,
    session_id: str | None = None,
    include_stale: bool = False,
    repo: MemoryEntryRepository | None = None,
) -> list[dict[str, Any]]:
    """Async wrapper: the SQLite/embedding/reranker work runs off the loop."""
    return await asyncio.to_thread(
        recall_memories,
        query,
        target,
        limit,
        char_budget,
        session_id=session_id,
        include_stale=include_stale,
        repo=repo,
    )
