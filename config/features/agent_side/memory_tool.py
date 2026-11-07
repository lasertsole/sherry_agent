"""Memory-tool store configuration.

Char caps for the three memory stores the ``memory`` tool owns: ``memory``
(agent notes), ``user`` (user profile) and ``facts`` (SYSTEM-LEVEL /
CROSS-PROJECT pitfalls and conventions). Char counts — not tokens — because
char counts are model-independent.

Also holds the aging window, the semantic-recall switch and the consolidation
cost gate. The three char caps, ``max_unused_days`` and the semantic settings are
UI-editable; the memory panel writes its overrides to the memory database and
these values remain the defaults.
"""

from typing import TypedDict


class MemoryToolConfig(TypedDict):
    """Memory-store limits (single source of truth for the defaults)."""

    memory_char_limit: int
    user_char_limit: int
    facts_char_limit: int
    #: Idle days before an entry becomes eligible for usage-based pruning.
    max_unused_days: int
    #: Semantic recall: embedding + reranker retrieval of relevant entries.
    semantic_recall_enabled: bool
    semantic_recall_top_k: int
    semantic_recall_char_budget: int
    #: Minimum active entries before the LLM consolidation pass runs; below it
    #: only the free usage-based aging pass runs.
    consolidation_min_entries: int


MEMORY_TOOL: MemoryToolConfig = {
    "memory_char_limit": 2_200,
    "user_char_limit": 1_375,
    "facts_char_limit": 1_375,
    "max_unused_days": 90,
    "semantic_recall_enabled": False,
    "semantic_recall_top_k": 5,
    "semantic_recall_char_budget": 4_000,
    "consolidation_min_entries": 10,
}
