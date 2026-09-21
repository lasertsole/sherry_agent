"""Embedding storage on top of the MesMemory database."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from context_engine.content_codec import decode_content
from context_engine.store.db import get_db

EmbeddingRow = dict[str, Any]


def save_embeddings(
    rows: list[tuple[str, int, list[float], str]],
) -> int:
    """Persist (session_id, message_id, embedding, model) tuples.

    Rows whose message_id already has an embedding are skipped (the UNIQUE
    constraint makes the write idempotent).

    Returns the number of rows actually inserted.
    """
    if not rows:
        return 0
    db = get_db()
    created = datetime.now().strftime("%Y%m%d%H%M%S")
    inserted = 0
    for session_id, message_id, embedding, model in rows:
        blob = _pack(embedding)
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO message_embeddings
                (session_id, message_id, embedding, model, dim, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, message_id, blob, model, len(embedding), created),
        )
        inserted += cursor.rowcount if cursor.rowcount > 0 else 0
    db.commit()
    return inserted


def load_all_embeddings(session_id: str | None = None) -> list[EmbeddingRow]:
    """Load every embedding (optionally scoped to one session), joined with
    the message text for scoring context."""
    db = get_db()
    base = """
        SELECT e.session_id, e.message_id, e.embedding, e.model,
               m.role, m.content, m.turn_num
        FROM message_embeddings e
        JOIN messages m ON m.id = e.message_id
    """
    params: tuple = ()
    if session_id is not None:
        base += " WHERE e.session_id = ?"
        params = (session_id,)
    rows = db.execute(base, params).fetchall()
    results: list[EmbeddingRow] = []
    for row in rows:
        results.append(
            {
                "session_id": row["session_id"],
                "message_id": row["message_id"],
                "embedding": _unpack(row["embedding"]),
                "model": row["model"],
                "role": row["role"],
                "content": _decode(row["content"]),
                "turn_num": row["turn_num"],
                "indexed_at": time.time(),
            }
        )
    return results


def _pack(vector: list[float]) -> bytes:
    import array

    return array.array("d", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    import array

    values = array.array("d")
    values.frombytes(blob)
    return values.tolist()


def _decode(content: Any) -> Any:
    return decode_content(content)
