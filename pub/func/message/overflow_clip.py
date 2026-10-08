"""Overflow tail clip (P1-2): the no-LLM first move on context pressure.

The cheapest recovery from context overflow is to shrink the *trailing
contiguous batch* of ``ToolMessage`` results: the newest tool outputs are
usually the largest context consumers and the most expendable. Every tool
result was already persisted to MesMemory the moment it returned
(``MessagePersistenceMiddleware`` flushes at tool return), so the full text
stays retrievable with the ``message_search`` tool; results offloaded by P0-2
keep their ``[evicted to: …]`` file pointer, and ``read_file`` results keep
their P2-4 slice notice.

This pass REPLACES a message's content with a compact stub via
``ToolMessage.model_copy``; it never removes, reorders, or injects messages.
Message identity (``id``), ``tool_call_id``, ``name`` and
``additional_kwargs`` survive, so the tool-call/result pairing stays intact
for ``sanitize_tool_use_result_pairing`` and the message-persistence
watermark keeps matching the same logical message.

Budget contract (the caller derives it from the shared estimators —
``estimate_messages_tokens`` vs the constructor-injected context window):
``target_tokens`` is the number of estimated tokens the clip should free.
Clipping stops as soon as the target is reached, the contiguous batch ends, or
``overflow_clip_max_remove`` messages were stubbed. ``target_tokens <= 0``
means the estimate sits on the threshold line, so the pass takes the maximum
eligible batch.

Idempotency: a stubbed message ends the scannable batch, so a second pass over
an already-clipped transcript is a no-op, and the P0-2 / P2-4 markers inside
the stub survive every repeat.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from config.features import SUMMARIZATION
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens
from pub.func.message.eviction import EVICTION_PREFIX, READ_FILE_SLICE_NOTICE

__all__ = ["CLIP_MARKER", "ROUTE_TAIL_CLIP", "clip_overflow_tail"]

ROUTE_TAIL_CLIP = "tail_clip"

CLIP_MARKER = "[overflow-clip]"

_READ_FILE_HINT = "Use read_file(file_path='{path}', offset=0, limit=100) to read the full content."


def _content_text(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _is_text_block(block: Any) -> bool:
    return isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")


def _evicted_path(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(EVICTION_PREFIX):
            path = line[len(EVICTION_PREFIX) :].removesuffix("]").strip()
            return path or None
    return None


def _build_stub_text(text: str) -> str:
    stub = (
        f"{CLIP_MARKER} tool result shrunk from {len(text)} chars to recover "
        "context overflow. The full output was persisted to session memory; "
        "use message_search to retrieve it."
    )
    path = _evicted_path(text)
    if path:
        stub += f"\n{EVICTION_PREFIX}{path}]\n{_READ_FILE_HINT.format(path=path)}"
    if READ_FILE_SLICE_NOTICE.strip() in text:
        stub += f"\n{READ_FILE_SLICE_NOTICE.strip()}"
    return stub


def _stubbed(message: ToolMessage, text: str) -> ToolMessage | None:
    """Return the stub replacement, or ``None`` when it would not shrink."""
    stub_text = _build_stub_text(text)
    content = message.content
    if isinstance(content, str):
        new_content: str | list[Any] = stub_text
    else:
        non_text = [block for block in content if not _is_text_block(block)]
        new_content = [{"type": "text", "text": stub_text}, *non_text]
    candidate = message.model_copy(update={"content": new_content})
    if estimate_msg_tokens(candidate) >= estimate_msg_tokens(message):
        return None
    return candidate


def clip_overflow_tail(
    messages: list[BaseMessage],
    target_tokens: int,
) -> list[BaseMessage] | None:
    """Shrink the trailing contiguous ``ToolMessage`` batch to compact stubs.

    Returns the rewritten list (same length, same message identities) or
    ``None`` when nothing was clipped — disabled config, a transcript at or
    below ``overflow_clip_min_keep`` messages, no trailing ``ToolMessage``
    batch, or every batch member already at/below stub size. ``None`` means
    the caller must degrade to the existing overflow route.
    """
    if not SUMMARIZATION["overflow_clip_enabled"]:
        return None

    min_keep = SUMMARIZATION["overflow_clip_min_keep"]
    max_remove = SUMMARIZATION["overflow_clip_max_remove"]
    if len(messages) <= min_keep or max_remove <= 0:
        return None

    result = list(messages)
    freed_total = 0
    clipped = 0
    changed = False

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, ToolMessage):
            break
        text = _content_text(message.content)
        if CLIP_MARKER in text:
            break
        stub = _stubbed(message, text)
        if stub is None:
            continue
        result[index] = stub
        changed = True
        clipped += 1
        freed_total += estimate_msg_tokens(message) - estimate_msg_tokens(stub)
        if clipped >= max_remove:
            break
        if target_tokens > 0 and freed_total >= target_tokens:
            break

    return result if changed else None
