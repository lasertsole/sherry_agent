"""Compression-time nudge settings.

The memory review is dispatched on every compression, and plan extraction fires
when the plan completes at a turn boundary.
"""

from typing import TypedDict


class NudgeConfig(TypedDict):
    """Compression-time nudge settings."""

    plan_extraction_enabled: bool
    #: How the memory review builds its input.
    #:
    #: - ``cache_warm`` (default): the discarded messages VERBATIM, on the main
    #:   model the turn just ran on. The compaction cut is a prefix cut, so that
    #:   slice is exactly the request prefix the main model just served — the
    #:   provider answers it from its prefix cache and the review reads the real
    #:   text. Costs cache-read prices for the prefix plus one fresh instruction.
    #: - ``digest``: the folded view (``summarization/digest.py``). For a
    #:   deployment whose provider caches prefixes poorly (or whose review model
    #:   has a small window), the fold bounds the input regardless of provider
    #:   behaviour, at the price of losing the older detail.
    memory_review_mode: str


NUDGE: NudgeConfig = {
    "plan_extraction_enabled": True,
    "memory_review_mode": "cache_warm",
}
