"""Backward-compatible re-export.

The canonical implementation now lives in :mod:`pub.func.estimate_tokens`
(three-tier fallback: API-reported → CJK-aware heuristic → legacy ``// 4``).
"""

from pub.func.estimate_tokens import (
    estimate_msg_tokens as estimate_msg_tokens,
    estimate_messages_tokens as estimate_messages_tokens,
)

__all__ = ["estimate_msg_tokens", "estimate_messages_tokens"]
