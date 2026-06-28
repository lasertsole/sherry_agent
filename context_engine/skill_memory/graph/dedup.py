"""
skill_memory — Vector Cosine Deduplication

Vector cosine deduplication — detect and merge semantically duplicate nodes.

Principle: If the cosine similarity of two nodes' embeddings exceeds the threshold,
they are considered duplicates.

Examples:
  - "conda-env-create" and "conda-create-environment" → same skill
  - "importerror-libgl1" and "libgl-missing-error" → same event

Merge strategy:
  - Keep the node with higher validated_count
  - Merge source_sessions
  - Migrate edges (both from/to point to the kept node)
  - Mark the merged node as deprecated

Complexity: O(n²) comparisons, n = nodes with vectors. Thousands of nodes < 50ms.
"""

import math
from ..type import GmConfig
from typing import TypedDict
from sqlite3 import Connection
from ..store.core import find_by_id, get_all_vectors, merge_nodes


class DuplicatePair(TypedDict):
    """Duplicate node pair"""
    node_a: str
    node_b: str
    name_a: str
    name_b: str
    similarity: float


class DedupResult(TypedDict):
    """Deduplication result"""
    pairs: list[DuplicatePair]
    merged: int


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Compute cosine similarity between two vectors.

    Args:
        a: Vector a
        b: Vector b

    Returns:
        Cosine similarity value (between 0 and 1)
    """
    min_len = min(len(a), len(b))
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for i in range(min_len):
        dot += a[i] * b[i]
        norm_a += a[i] * a[i]
        norm_b += b[i] * b[i]

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b) + 1e-9)


def detect_duplicates(db: Connection, cfg: GmConfig) -> list[DuplicatePair]:
    """
    Detect duplicate node pairs.

    Requires embeddings to work — nodes without vectors are skipped.
    Exact name matching via FTS5 is already handled by store.upsert_node.
    This function handles semantic duplicates.

    Args:
        db: SQLite database connection
        cfg: Graph Memory configuration

    Returns:
        List of duplicate pairs sorted by similarity (descending)
    """
    vectors_data = get_all_vectors(db)

    if len(vectors_data) < 2:
        return []

    threshold = getattr(cfg, 'dedup_threshold', 0.90)
    pairs: list[DuplicatePair] = []

    for i in range(len(vectors_data)):
        for j in range(i + 1, len(vectors_data)):
            vec_i = vectors_data[i]['embedding']
            vec_j = vectors_data[j]['embedding']

            sim = cosine_similarity(vec_i, vec_j)

            if sim >= threshold:
                node_a = find_by_id(db, vectors_data[i]['node_id'])
                node_b = find_by_id(db, vectors_data[j]['node_id'])

                if node_a and node_b:
                    pairs.append({
                        'node_a': node_a.id,
                        'node_b': node_b.id,
                        'name_a': node_a.name,
                        'name_b': node_b.name,
                        'similarity': sim,
                    })

    # Sort by similarity descending
    return sorted(pairs, key=lambda x: x['similarity'], reverse=True)


def dedup(db: Connection, cfg: GmConfig) -> DedupResult:
    """
    Detect and automatically merge duplicate nodes.

    Merge rules:
      - Only merge nodes of the same type (SKILL+SKILL, EVENT+EVENT)
      - Keep the node with higher validated_count
      - When validated_count is equal, keep the more recently updated node

    Args:
        db: SQLite database connection
        cfg: Graph Memory configuration

    Returns:
        Dictionary containing duplicate pairs and merge count
    """
    pairs = detect_duplicates(db, cfg)
    merged = 0

    # Nodes already merged are excluded from further merging
    consumed = set()

    for pair in pairs:
        if pair['node_a'] in consumed or pair['node_b'] in consumed:
            continue

        node_a = find_by_id(db, pair['node_a'])
        node_b = find_by_id(db, pair['node_b'])

        if not node_a or not node_b:
            continue

        # Only merge nodes of the same type
        if node_a.type != node_b.type:
            continue

        # Decide which node to keep
        keep_id: str
        merge_id: str

        if node_a.validated_count > node_b.validated_count:
            keep_id = node_a.id
            merge_id = node_b.id
        elif node_b.validated_count > node_a.validated_count:
            keep_id = node_b.id
            merge_id = node_a.id
        else:
            # When validated_count is equal, keep the more recently updated node
            if node_a.updated_at >= node_b.updated_at:
                keep_id = node_a.id
                merge_id = node_b.id
            else:
                keep_id = node_b.id
                merge_id = node_a.id

        merge_nodes(db, keep_id, merge_id)
        consumed.add(merge_id)
        merged += 1

    return {
        'pairs': pairs,
        'merged': merged,
    }
