"""Lazy embedding generation for MesMemory messages (SESSION plan P2-5)."""

from __future__ import annotations

from collections.abc import Callable

from context_engine.store.db import get_db
from context_engine.embeddings.store import save_embeddings

_EMBED_MODEL_NAME = "bge-m3"
_EMBED_DIM = 1024
_BATCH_SIZE = 32

_embed_fn: Callable[[list[str]], list[list[float]]] | None = None


def set_embed_fn(fn: Callable[[list[str]], list[list[float]]]) -> None:
    """Override the embedding backend (used by tests and alternative models)."""
    global _embed_fn
    _embed_fn = fn


def _get_embed_fn() -> Callable[[list[str]], list[list[float]]]:
    global _embed_fn
    if _embed_fn is None:
        from models import build_embed_model

        model = build_embed_model()
        _embed_fn = model.embed_documents
    return _embed_fn


def index_pending_messages(session_id: str | None = None) -> int:
    """Generate embeddings for messages that do not have one yet.

    Idempotent: messages with an existing embedding row are skipped (LEFT
    JOIN miss drives the pending set). Returns the number embedded.
    """
    db = get_db()
    base = """
        SELECT m.id, m.session_id, m.content, m.role
        FROM messages m
        LEFT JOIN message_embeddings e ON e.message_id = m.id
        WHERE e.id IS NULL AND m.role IN ('human', 'ai')
    """
    params: tuple = ()
    if session_id is not None:
        base += " AND m.session_id = ?"
        params = (session_id,)
    pending = db.execute(base + " ORDER BY m.id ASC LIMIT 500", params).fetchall()
    if not pending:
        return 0

    embed = _get_embed_fn()
    embedded = 0
    for start in range(0, len(pending), _BATCH_SIZE):
        batch = pending[start : start + _BATCH_SIZE]
        texts = [_content_text(row["content"]) for row in batch]
        vectors = embed(texts)
        rows = [
            (row["session_id"], row["id"], vector, _EMBED_MODEL_NAME)
            for row, vector in zip(batch, vectors)
        ]
        embedded += save_embeddings(rows)
    return embedded


def _content_text(content: object) -> str:
    import json

    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)
