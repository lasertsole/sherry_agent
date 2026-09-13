"""Cosine-similarity semantic search over message embeddings."""

from __future__ import annotations

from typing import Any

import numpy as np

from context_engine.embeddings.indexer import _get_embed_fn, index_pending_messages
from context_engine.embeddings.store import load_all_embeddings


async def semantic_search(
    query: str,
    session_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Embed the query, self-heal the index, then rank messages by cosine
    similarity. Returns the top matches with score and message metadata."""
    if not query.strip():
        return []

    index_pending_messages(session_id)
    candidates = load_all_embeddings(session_id)
    if not candidates:
        return []

    query_vector = np.asarray(_get_embed_fn()([query])[0], dtype=np.float32)
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        vector = np.asarray(candidate["embedding"], dtype=np.float32)
        norm = float(np.linalg.norm(query_vector) * np.linalg.norm(vector))
        score = float(np.dot(query_vector, vector) / norm) if norm else 0.0
        scored.append((score, candidate))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    return [
        {
            "session_id": c["session_id"],
            "message_id": c["message_id"],
            "turn_num": c["turn_num"],
            "role": c["role"],
            "content": c["content"],
            "score": round(score, 4),
        }
        for score, c in scored[:limit]
    ]
