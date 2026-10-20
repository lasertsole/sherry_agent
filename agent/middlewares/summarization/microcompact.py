"""Microcompact — clear OLD tool results locally, without an LLM call (ZCode parity).

A cheap preventive layer that runs before the LLM summarization route: once the
session has been idle for ``microcompact_idle_threshold_minutes`` or token
pressure reaches the microcompact threshold, the oldest tool results in the
compactable-tool set are replaced by a short marker. The most recent
``microcompact_keep_recent_tool_results`` messages always stay intact, media
results are never touched, and a pass that saves fewer than
``microcompact_min_token_savings`` tokens is discarded wholesale.

The marker keeps the ``ToolMessage`` pairing intact (``tool_call_id`` / name /
status survive) and ends the scannable tail like the overflow tail clip, so the
existing sanitizer and persistence watermark stay satisfied.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, ToolMessage
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens

from config.features import SUMMARIZATION

CLEARED_MARKER = "[Old tool result content cleared]"

_KEEP_RECENT = SUMMARIZATION["microcompact_keep_recent_tool_results"]
_IDLE_MINUTES = SUMMARIZATION["microcompact_idle_threshold_minutes"]
_MIN_SAVINGS = SUMMARIZATION["microcompact_min_token_savings"]
_THRESHOLD_RATIO = SUMMARIZATION["microcompact_threshold_ratio"]
_BUFFER_TOKENS = SUMMARIZATION["microcompact_buffer_tokens"]
_COMPACTABLE = SUMMARIZATION["microcompact_compactable_tools"]

_MEDIA_BLOCK_TYPES = frozenset(
    {
        "image",
        "image_url",
        "input_image",
        "video",
        "video_url",
        "audio",
        "input_audio",
        "file",
        "input_file",
    }
)


def _has_media(content) -> bool:
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict):
            if block.get("type") in _MEDIA_BLOCK_TYPES:
                return True
            if any(key.endswith("_url") for key in block):
                return True
    return False


def _is_cleared(content) -> bool:
    if isinstance(content, str):
        return CLEARED_MARKER in content
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and CLEARED_MARKER in str(block.get("text", ""))
            for block in content
        )
    return False


def resolve_microcompact_trigger(
    messages: list[BaseMessage],
    *,
    auto_compact_threshold: int,
    last_assistant_completed_at_ms: float | None = None,
    now_ms: float | None = None,
) -> str | None:
    """``"time_based"`` / ``"token_pressure"`` / ``None`` (ZCode's two triggers)."""
    if (
        last_assistant_completed_at_ms is not None
        and now_ms is not None
        and now_ms - last_assistant_completed_at_ms >= _IDLE_MINUTES * 60_000
    ):
        return "time_based"

    if auto_compact_threshold <= 0:
        return None
    threshold = min(
        int(auto_compact_threshold * _THRESHOLD_RATIO),
        max(0, auto_compact_threshold - _BUFFER_TOKENS),
    )
    if sum(estimate_msg_tokens(message) for message in messages) >= threshold:
        return "token_pressure"
    return None


def maybe_local_microcompact(
    messages: list[BaseMessage],
    *,
    auto_compact_threshold: int,
    last_assistant_completed_at_ms: float | None = None,
    now_ms: float | None = None,
) -> tuple[list[BaseMessage], int]:
    """Return ``(messages, saved_tokens)``; a no-op returns the original list."""
    if not SUMMARIZATION["microcompact_enabled"] or not messages:
        return messages, 0
    if (
        resolve_microcompact_trigger(
            messages,
            auto_compact_threshold=auto_compact_threshold,
            last_assistant_completed_at_ms=last_assistant_completed_at_ms,
            now_ms=now_ms,
        )
        is None
    ):
        return messages, 0

    keep_from = len(messages) - _KEEP_RECENT
    updated = list(messages)
    saved = 0
    for index, message in enumerate(messages):
        if index >= keep_from:
            break
        if not isinstance(message, ToolMessage):
            continue
        if (message.name or "") not in _COMPACTABLE:
            continue
        if _has_media(message.content) or _is_cleared(message.content):
            continue
        before = estimate_msg_tokens(message)
        replacement = message.model_copy(update={"content": CLEARED_MARKER})
        saved += max(0, before - estimate_msg_tokens(replacement))
        updated[index] = replacement

    if saved < _MIN_SAVINGS:
        return messages, 0
    return updated, saved
