"""Tiered memory (LT-1) facts-layer limits and category set."""

from typing import TypedDict


class TieredMemoryConfig(TypedDict):
    """Facts-layer per-file limits and the fixed category set."""

    facts_char_limit: int
    facts_index_max_chars: int
    facts_categories: tuple[str, ...]


TIERED_MEMORY: TieredMemoryConfig = {
    "facts_char_limit": 4000,
    "facts_index_max_chars": 200,
    "facts_categories": (
        "environment",
        "project",
        "decisions",
        "user_prefs",
        "tool_lessons",
    ),
}
