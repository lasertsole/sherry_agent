"""CJK-aware token estimation with three-tier fallback.

Three tiers (highest priority first):

  **Tier 1 — API-reported token usage**
      ``usage_metadata["input_tokens"]`` from the last ``AIMessage`` in the
      message list.  This is the provider's ground-truth count, captured at
      model-call time.  When available it short-circuits all local
      estimation.

  **Tier 2 — CJK-aware heuristic**
      Splits text into CJK characters (≈ ``len // CHARS_PER_TOKEN_CJK``)
      and non-CJK characters (≈ ``len // CHARS_PER_TOKEN``).  The existing
      ``pub.func.cjk.count_cjk`` helper is reused for CJK detection.
      For pure ASCII text this degenerates to the legacy formula, so
      existing ASCII-based tests keep the same numbers.

  **Tier 3 — Legacy ``len(text) // CHARS_PER_TOKEN``**
      Not a separate code path — it is the degenerate case of Tier 2 when
      ``count_cjk(text) == 0``.  Documented for clarity only.

Usage::

    from pub.func.estimate_tokens import estimate_messages_tokens

    # Auto: Tier 1 if last AIMessage carries usage_metadata, else Tier 2.
    tokens = estimate_messages_tokens(messages)

    # Explicit Tier 1 override (e.g. from a model response, not from messages).
    tokens = estimate_messages_tokens(messages, reported_tokens=8192)

    # Force Tier 2 only (disable Tier 1 auto-extract).
    tokens = estimate_messages_tokens(messages, reported_tokens=0)

For callers that need ``max(local_estimate, reported)`` semantics (e.g.
``overflow_router.compute_pressure``), pass ``reported_tokens=0`` to get
the pure local estimate and let the caller apply ``max()`` itself.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from config.features import TOKEN_ESTIMATION
from pub.func.cjk import count_cjk

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]


def estimate_text_tokens(text: str) -> int:
    cjk = count_cjk(text)
    non_cjk = len(text) - cjk
    return (cjk // CHARS_PER_TOKEN_CJK) + (non_cjk // CHARS_PER_TOKEN)


def extract_reported_tokens(messages: Sequence[Any]) -> int | None:
    for msg in reversed(list(messages)):
        if isinstance(msg, AIMessage):
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, dict):
                val = usage.get("input_tokens")
                if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    return val
                val = usage.get("total_tokens")
                if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    return val
            return None
    return None


def estimate_msg_tokens(msg: BaseMessage) -> int:
    total = 0
    content = msg.content

    if isinstance(content, str):
        total += estimate_text_tokens(content)
    else:
        total += estimate_text_tokens(json.dumps(content)) if content is not None else 0

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            total += estimate_text_tokens(str(tc.get("name", "")))
            total += estimate_text_tokens(str(tc.get("args", "")))

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        total += estimate_text_tokens(str(tool_call_id))

    return total


def estimate_messages_tokens(
    messages: Sequence[Any],
    reported_tokens: int | None = None,
) -> int:
    if reported_tokens is not None and reported_tokens > 0:
        return reported_tokens
    if reported_tokens is None:
        auto = extract_reported_tokens(messages)
        if auto is not None and auto > 0:
            return auto
    return sum(estimate_msg_tokens(m) for m in messages)
