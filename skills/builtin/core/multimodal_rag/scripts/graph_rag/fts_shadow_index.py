"""SQLite FTS5 shadow index for entities_vdb only.

Mirrors entity_name + content + keywords into FTS5 (unicode61 + trigram dual
tables) so entities_vdb.query() can combine keyword matching with vector
similarity. Only entities get FTS5 — relationships (prose) and chunks (long
text) stay pure vector.

The index is a shadow copy: it never replaces the SNKV vector store. Writes are
synchronous with upsert/delete so the two stores stay consistent within one
call. The optional trigram table covers CJK and identifier substrings that the
unicode61 tokenizer splits poorly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pub.func.cjk import contains_cjk


class EntitiesFtsIndex:
    """FTS5 shadow index for entities (unicode61 + trigram dual tables)."""

    def __init__(self, db_path: Path | str):
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                id, entity_name, content, keywords, tokenize='unicode61'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts_trigram USING fts5(
                id, entity_name, content, keywords, tokenize='trigram'
            );
            """
        )
        self._db.commit()

    def sync_upsert(self, items: dict[str, dict[str, Any]]) -> None:
        """Mirror SNKV entity upsert into FTS5 (delete + insert per id)."""
        for item_id, data in items.items():
            name = str(data.get("entity_name") or "")
            content = str(data.get("content") or "")
            keywords = str(data.get("keywords") or "")
            for table in ("entities_fts", "entities_fts_trigram"):
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
                self._db.execute(
                    f"INSERT INTO {table} (id, entity_name, content, keywords) VALUES (?, ?, ?, ?)",
                    (item_id, name, content, keywords),
                )
        self._db.commit()

    def sync_delete(self, ids: list[str]) -> None:
        for item_id in ids:
            for table in ("entities_fts", "entities_fts_trigram"):
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        self._db.commit()

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """FTS5 keyword search. CJK → trigram, otherwise → unicode61."""
        query = query.strip()
        if not query:
            return []

        if contains_cjk(query):
            table = "entities_fts_trigram"
            tokens = [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in query.split() if t]
            fts_query = (
                " OR ".join(tokens) if tokens else f'"{query.replace(chr(34), chr(34) * 2)}"'
            )
        else:
            table = "entities_fts"
            fts_query = query

        try:
            rows = self._db.execute(
                f"SELECT id FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_k),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [{"id": r["id"], "distance": 1.0 / (i + 1)} for i, r in enumerate(rows)]

    def count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM entities_fts").fetchone()[0])

    def close(self) -> None:
        self._db.close()


def merge_fts_vector(
    fts_hits: list[dict[str, Any]],
    vec_hits: list[dict[str, Any]],
    fts_weight: float = 0.4,
    vec_weight: float = 0.6,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Merge FTS5 and vector results by weighted score.

    FTS hits are ranked by reciprocal rank; vector hits keep their cosine
    score. An id present in both accumulates both weights, which is what makes
    an exact identifier hit beat a merely similar description.
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}

    # Keep the RICH vector metadata (entity_name/content/source_id/...) for
    # ids present in both stores. Downstream ``_get_node_data`` reads
    # ``entity_name`` from each hit; FTS rows only carry id + rank.
    for hit in vec_hits:
        eid = str(hit["id"])
        meta[eid] = dict(hit)
        scores[eid] = scores.get(eid, 0.0) + vec_weight * float(hit.get("distance", 0.0))

    for i, hit in enumerate(fts_hits):
        eid = str(hit["id"])
        scores[eid] = scores.get(eid, 0.0) + fts_weight * (1.0 / (i + 1))
        if eid not in meta:
            meta[eid] = dict(hit)

    merged = sorted(scores.items(), key=lambda item: -item[1])[:top_k]
    return [{**meta[eid], "distance": score} for eid, score in merged]
