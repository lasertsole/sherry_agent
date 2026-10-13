"""Memory-tool store configuration.

Char caps for the three single-file memory stores the ``memory`` tool owns:
MEMORY.md (agent notes), USER.md (user profile) and FACTS.md (broad,
module-independent pitfalls and conventions). Char counts — not tokens —
because char counts are model-independent.
"""

from typing import TypedDict


class MemoryToolConfig(TypedDict):
    """Char caps for the three single-file memory stores (single source of truth)."""

    memory_char_limit: int
    user_char_limit: int
    facts_char_limit: int


MEMORY_TOOL: MemoryToolConfig = {
    "memory_char_limit": 2_200,
    "user_char_limit": 1_375,
    "facts_char_limit": 1_375,
}
