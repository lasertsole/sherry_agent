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

Multimodal content lists are counted **per block**, never by JSON-serializing
the whole list: text blocks are estimated as text, media blocks
(``image_url`` / ``audio_url`` / ``video_url`` / ``audio_bytes`` /
``video_bytes`` / any block carrying a ``data:`` payload) get a fixed
per-type cost from ``TOKEN_ESTIMATION``, and unknown blocks get the
conservative unknown-block cost.  Turn that around and a 5 MB base64 image
would read as ~1.25M "tokens" and trigger compression on its own.

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

import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from config.features import TOKEN_ESTIMATION
from pub.func.cjk import count_cjk

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]
TOKENS_PER_IMAGE_BLOCK = TOKEN_ESTIMATION["tokens_per_image_block"]
TOKENS_PER_AUDIO_BLOCK = TOKEN_ESTIMATION["tokens_per_audio_block"]
TOKENS_PER_VIDEO_BLOCK = TOKEN_ESTIMATION["tokens_per_video_block"]
TOKENS_PER_UNKNOWN_BLOCK = TOKEN_ESTIMATION["tokens_per_unknown_block"]

# Declared content-block types per media family. ``image`` / ``audio`` /
# ``video`` are the LangChain shapes; the ``*_url`` / ``*_bytes`` names are the
# OpenAI/Sherry shapes.
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_AUDIO_BLOCK_TYPES = frozenset({"audio", "audio_url", "audio_bytes", "input_audio"})
_VIDEO_BLOCK_TYPES = frozenset({"video", "video_url", "video_bytes"})

_FAMILY_TOKENS = {
    "image": TOKENS_PER_IMAGE_BLOCK,
    "audio": TOKENS_PER_AUDIO_BLOCK,
    "video": TOKENS_PER_VIDEO_BLOCK,
}


def estimate_text_tokens(text: str) -> int:
    cjk = count_cjk(text)
    non_cjk = len(text) - cjk
    return (cjk // CHARS_PER_TOKEN_CJK) + (non_cjk // CHARS_PER_TOKEN)


def _data_url_family(block: dict[str, Any]) -> str | None:
    """Return the media family of a ``data:`` payload inside *block*, if any.

    Walks the known URL shapes (top-level ``url`` and the nested
    ``image_url`` / ``audio_url`` / ``video_url`` dicts) plus the DeepAgents
    ``base64`` field, and classifies by the data-URL mime prefix. A data URL
    with an unrecognised mime — or a bare ``base64`` field — reports
    ``"unknown"`` so the caller still charges a fixed media cost.
    """
    for key in ("url", "image_url", "audio_url", "video_url"):
        value = block.get(key)
        candidate: str | None = None
        if isinstance(value, str):
            candidate = value
        elif isinstance(value, dict):
            nested = value.get("url")
            if isinstance(nested, str):
                candidate = nested
        if candidate is not None and candidate.startswith("data:"):
            mime = candidate[5:].split(";", 1)[0].split(",", 1)[0].strip().lower()
            if mime.startswith("image/"):
                return "image"
            if mime.startswith("audio/"):
                return "audio"
            if mime.startswith("video/"):
                return "video"
            return "unknown"
    base64_value = block.get("base64")
    if isinstance(base64_value, str) and base64_value.strip():
        return "unknown"
    return None


def classify_media_family(block: dict[str, Any]) -> str | None:
    """Classify a non-text content block: media family or ``None``.

    Declared block types win; a block whose type is unknown but that carries a
    ``data:`` / ``base64`` payload is treated as media all the same. Returns
    one of ``"image"`` / ``"audio"`` / ``"video"`` / ``"unknown"``, or ``None``
    for a block that is not media.
    """
    block_type = block.get("type", "")
    if block_type in _IMAGE_BLOCK_TYPES:
        return "image"
    if block_type in _AUDIO_BLOCK_TYPES:
        return "audio"
    if block_type in _VIDEO_BLOCK_TYPES:
        return "video"
    return _data_url_family(block)


def estimate_content_tokens(content: Any) -> int:
    """Estimate the tokens of a message ``content`` payload.

    ``str`` content keeps the exact legacy estimate. ``None`` is 0. A content
    list is counted per block: text blocks are concatenated and estimated as
    text (a single text block therefore matches the same string content), and
    every media/unknown block adds its fixed per-type cost instead of its raw
    (possibly base64) character length. JSON serialization of the list is
    deliberately gone — it is what used to count base64 as text.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, dict):
        return estimate_content_tokens([content])
    if not isinstance(content, (list, tuple)):
        # Unexpected scalar content: fall back to its string form.
        return estimate_text_tokens(str(content))

    text_parts: list[str] = []
    fixed_tokens = 0
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            continue
        if not isinstance(block, dict):
            fixed_tokens += TOKENS_PER_UNKNOWN_BLOCK
            continue
        fixed_tokens += _estimate_block_tokens(block, text_parts)
    if text_parts:
        fixed_tokens += estimate_text_tokens("\n".join(text_parts))
    return fixed_tokens


def _estimate_block_tokens(block: dict[str, Any], text_parts: list[str]) -> int:
    """Accumulate one block's cost; text goes to *text_parts*, media is fixed.

    Returns the fixed token cost of a non-text block (0 for a text block,
    whose text is appended to *text_parts* instead).
    """
    if block.get("type") == "text":
        text = block.get("text")
        if isinstance(text, str):
            text_parts.append(text)
        return 0
    family = classify_media_family(block)
    if family is None:
        return TOKENS_PER_UNKNOWN_BLOCK
    return _FAMILY_TOKENS.get(family, TOKENS_PER_UNKNOWN_BLOCK)


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
    total = estimate_content_tokens(msg.content)

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
