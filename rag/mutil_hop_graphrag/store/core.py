import json
import sqlite3
import numpy as np
from .db import get_db
from typing import List, Tuple, Dict
from ..type import SearchMatch, GraphEdge


class PersistentGraph:
    """
    Persistent graph with SQLite storage and hybrid search (embedding + FTS5).

    Features:
    - Stores nodes and edges in SQLite database
    - FTS5 full-text search on node text
    - Vector similarity search using embeddings
    - Hybrid ranking combining both signals
    - Automatic persistence across sessions
    """

    def __init__(self):
        self.conn = get_db()
        self.conn.row_factory = sqlite3.Row

        # Cache for embeddings (for fast vector search)
        self._node_ids: List[int] = []
        self._node_embeddings: np.ndarray = np.empty((0, 0))
        self._node_texts: Dict[int, str] = {}

    def add_node(self, node_id: int, text: str, embedding: np.ndarray):
        """Add a node with its embedding to the database.
        Note: FTS indexing is handled automatically by triggers.
        """
        cursor = self.conn.cursor()

        # Store embedding as JSON
        embedding_json = json.dumps(embedding.tolist())

        # Insert into main table (Triggers will handle FTS updates)
        cursor.execute(
            "INSERT OR REPLACE INTO nodes (node_id, text, embedding_json) VALUES (?, ?, ?)",
            (node_id, text, embedding_json)
        )

        self.conn.commit()

        # Update cache
        self._node_texts[node_id] = text
        self._invalidate_embedding_cache()

    def add_edge(self, source_id: int, target_id: int, bridge_relation: str):
        """Add an edge between two nodes."""
        if source_id not in self._node_texts or target_id not in self._node_texts:
            raise ValueError(f"Node {source_id} or {target_id} not found")

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO edges (source_id, target_id, bridge_relation) VALUES (?, ?, ?)",
            (source_id, target_id, bridge_relation)
        )
        self.conn.commit()

    def get_out_edges(self, node_id: int) -> List[GraphEdge]:
        """Get all outgoing edges from a node."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT source_id, target_id, bridge_relation FROM edges WHERE source_id = ?",
            (node_id,)
        )
        return [GraphEdge(row['source_id'], row['target_id'], row['bridge_relation'])
                for row in cursor.fetchall()]

    def build_embedding_index(self):
        """Build in-memory embedding index for fast vector search."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT node_id, embedding_json FROM nodes ORDER BY node_id")

        ids = []
        embs = []
        texts = {}

        for row in cursor.fetchall():
            nid = row['node_id']
            emb = np.array(json.loads(row['embedding_json']))
            ids.append(nid)
            embs.append(emb)
            texts[nid] = self._node_texts.get(nid, "")

        self._node_ids = ids
        self._node_embeddings = np.array(embs) if embs else np.empty((0, 0))
        self._node_texts.update(texts)

    def _invalidate_embedding_cache(self):
        """Mark embedding cache as stale."""
        self._node_ids = []
        self._node_embeddings = np.empty((0, 0))

    def hybrid_search(
            self,
            query: str,
            query_embedding: np.ndarray,
            top_k: int = 5,
            embedding_weight: float = 0.6,
            fts_weight: float = 0.4
    ) -> List[SearchMatch]:
        """
        Hybrid search combining vector similarity and full-text search.

        Args:
            query: Search query string
            query_embedding: Query embedding vector
            top_k: Number of results to return
            embedding_weight: Weight for embedding similarity (0-1)
            fts_weight: Weight for FTS score (0-1)

        Returns:
            List of SearchMatch objects sorted by combined score
        """
        # Ensure embedding cache is built
        if len(self._node_ids) == 0:
            self.build_embedding_index()

        if len(self._node_ids) == 0:
            return []

        # 1. Vector similarity search
        qn = query_embedding / np.linalg.norm(query_embedding)
        nn = self._node_embeddings / np.linalg.norm(
            self._node_embeddings, axis=1, keepdims=True
        )
        embedding_sims = nn @ qn

        # Normalize embedding scores to [0, 1]
        if embedding_sims.max() != embedding_sims.min():
            emb_scores_normalized = (embedding_sims - embedding_sims.min()) / (
                    embedding_sims.max() - embedding_sims.min()
            )
        else:
            emb_scores_normalized = np.zeros_like(embedding_sims)

        # 2. FTS5 full-text search
        cursor = self.conn.cursor()
        # FTS5 requires direct string concatenation for MATCH (no parameterized queries)
        # Escape special FTS characters to prevent syntax errors
        escaped_query = query.replace('"', '""')
        fts_query = f'"{escaped_query}"'

        cursor.execute(
            f"SELECT rowid, rank FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, top_k * 2)  # Get more candidates for reranking
        )
        fts_results = cursor.fetchall()

        # Normalize FTS scores (lower rank = better match)
        fts_score_map = {}
        if fts_results:
            ranks = [row['rank'] for row in fts_results]
            max_rank = max(ranks)
            min_rank = min(ranks)

            for row in fts_results:
                nid = row['rowid']
                # Invert rank so higher is better, normalize to [0, 1]
                if max_rank != min_rank:
                    fts_score = 1.0 - (row['rank'] - min_rank) / (max_rank - min_rank)
                else:
                    fts_score = 1.0
                fts_score_map[nid] = fts_score

        # 3. Combine scores
        matches = []
        for i, nid in enumerate(self._node_ids):
            emb_score = float(emb_scores_normalized[i])
            fts_score = fts_score_map.get(nid, 0.0)

            # Combined score
            combined = embedding_weight * emb_score + fts_weight * fts_score

            matches.append(SearchMatch(
                node_id=nid,
                text=self._node_texts.get(nid, ""),
                embedding_score=emb_score,
                fts_score=fts_score,
                combined_score=combined,
                match_type="hybrid" if fts_score > 0 else "embedding"
            ))

        # Sort by combined score and return top_k
        matches.sort(key=lambda x: x.combined_score, reverse=True)
        return matches[:top_k]

    def search_entry_nodes(
            self,
            query: str,
            query_embedding: np.ndarray,
            top_k: int = 3,
            use_hybrid: bool = True
    ) -> List[Tuple[int, float]]:
        """
        Search for entry nodes using hybrid or pure embedding search.

        Args:
            query: Search query
            query_embedding: Query embedding
            top_k: Number of results
            use_hybrid: Whether to use hybrid search (default True)

        Returns:
            List of (node_id, score) tuples
        """
        if use_hybrid:
            matches = self.hybrid_search(query, query_embedding, top_k=top_k)
            return [(m.node_id, m.combined_score) for m in matches]
        else:
            # Fallback to pure embedding search
            if not self._node_ids:
                return []
            qn = query_embedding / np.linalg.norm(query_embedding)
            nn = self._node_embeddings / np.linalg.norm(
                self._node_embeddings, axis=1, keepdims=True
            )
            sims = nn @ qn
            top_idx = np.argsort(sims)[::-1][:top_k]
            return [(self._node_ids[i], float(sims[i])) for i in top_idx]

    def node_text(self, nid: int) -> str:
        """Get text for a node."""
        if nid in self._node_texts:
            return self._node_texts[nid]

        cursor = self.conn.cursor()
        cursor.execute("SELECT text FROM nodes WHERE node_id = ?", (nid,))
        row = cursor.fetchone()
        if row:
            text = row['text']
            self._node_texts[nid] = text
            return text
        return ""

    def get_stats(self) -> Dict:
        """Get database statistics."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM nodes")
        num_nodes = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM edges")
        num_edges = cursor.fetchone()[0]

        return {
            "num_nodes": num_nodes,
            "num_edges": num_edges,
        }

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()