"""Lazy embeddings for long-term memory entries.

The table already reserves ``embedding`` / ``embedding_model`` /
``embedding_dim`` (the Phase-1 schema), so the semantic-recall path only needs
an indexer and a loader. Embeddings are generated on demand from the same
``bge-m3`` backend the message store uses — no extra model, no second cache.

Rows are only embedded while they are ACTIVE; archiving a row leaves its vector
in place but the loader skips it, and editing an entry (``update_entry``) nulls
the vector so the next pass re-embeds the new text.
"""

from __future__ import annotations

import logging

from context_engine.embeddings.indexer import _get_embed_fn
from context_engine.store.memory_entry_repository import (
    MemoryEntryRepository,
    memory_entry_repo,
)

__all__ = [
    "MEMORY_EMBED_MODEL",
    "index_pending_memory_entries",
    "load_memory_embeddings",
]

logger = logging.getLogger(__name__)

#: Embedding model recorded next to each vector (bge-m3, 1024 dimensions).
MEMORY_EMBED_MODEL = "bge-m3"
EMBED_BATCH_SIZE = 32
EMBED_PENDING_LIMIT = 500


def index_pending_memory_entries(
    repo: MemoryEntryRepository | None = None,
    *,
    limit: int = EMBED_PENDING_LIMIT,
) -> int:
    """Embed active entries whose vector is missing. Returns the count written.

    Idempotent: the pending set is driven by ``embedding IS NULL``. A batch
    failure is logged and stops the pass — the next recall retries; a partial
    batch never leaves rows half-written because each UPDATE is idempotent.
    """
    repository = repo or memory_entry_repo
    pending = repository.list_missing_embeddings(limit=limit)
    if not pending:
        return 0

    try:
        embed = _get_embed_fn()
    except Exception:  # noqa: BLE001 - an unavailable embedder disables recall
        logger.exception("memory embeddings: embedder unavailable")
        return 0

    written = 0
    for start in range(0, len(pending), EMBED_BATCH_SIZE):
        batch = pending[start : start + EMBED_BATCH_SIZE]
        try:
            vectors = embed([str(entry["content"]) for entry in batch])
        except Exception:  # noqa: BLE001 - one failed batch must not kill the caller
            logger.exception("memory embeddings: batch failed at offset {}", start)
            break
        if len(vectors) != len(batch):
            logger.warning(
                "memory embeddings: embedder returned {} vectors for {} texts",
                len(vectors),
                len(batch),
            )
            break
        for entry, vector in zip(batch, vectors):
            repository.set_embedding(
                int(entry["id"]),
                [float(value) for value in vector],
                model=MEMORY_EMBED_MODEL,
            )
            written += 1
    return written


def load_memory_embeddings(
    target: str | None = None,
    repo: MemoryEntryRepository | None = None,
) -> list[dict[str, object]]:
    """Load active entries with their vectors, oldest first.

    Each item is the entry's row plus an ``embedding`` key (a list of floats).
    Archived rows are excluded by the repository query.
    """
    repository = repo or memory_entry_repo
    return [
        {**entry, "embedding": vector} for entry, vector in repository.list_with_embeddings(target)
    ]
