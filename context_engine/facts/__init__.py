"""Facts extraction pipeline (SESSION plan P2-3).

Dual-watermark cursors guarantee crash-safe at-least-once consumption of
persisted turns; the extractor runs the auxiliary LLM over a turn range and
writes durable facts through the existing tiered facts store (LT-1).
"""

from .cursor import advance_consumed, advance_enqueued, get_cursor, get_pending
from .queue import enqueue_turn, process_pending

__all__ = [
    "advance_consumed",
    "advance_enqueued",
    "enqueue_turn",
    "get_cursor",
    "get_pending",
    "process_pending",
]
