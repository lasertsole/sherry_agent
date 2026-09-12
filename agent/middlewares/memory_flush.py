"""Pre-compression Memory Flush (P0-1).

Before the summarization middleware calls the main LLM to compact history, a
cheap extraction model scans the messages that are about to be discarded and
writes persistent cross-session facts to MEMORY.md. Compaction then proceeds
unchanged; the flush is a best-effort enhancement that never blocks it.

Reference: openclaw compaction.memoryFlush.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Any

from config.features import MEMORY_FLUSH

logger = logging.getLogger(__name__)

_FLUSH_PROMPT = """\
You are a memory extraction assistant. Below is conversation history that is \
about to be compacted (discarded). Extract **persistent facts** that would be \
useful in future sessions.

Extraction rules:
1. Only extract cross-session facts: user preferences, project conventions, \
key decisions, environment facts, tool lessons learned.
2. Do NOT extract temporary task progress (that is the summary's job).
3. Each fact on one line, prefixed with a category.
4. If nothing worth extracting, output a single line: "(none)".

Format (use § to separate entries):
§ Environment: <fact>
§ Project: <fact>
§ Decision: <fact>
§ User: <fact>
§ Tool: <fact>

Conversation to be discarded:
{discarded_text}
"""


def should_flush(discarded_messages: Sequence[Any], estimated_tokens: int) -> bool:
    """Return True when the discarded history is large enough to justify a flush."""
    if not MEMORY_FLUSH["enabled"]:
        return False
    total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
    if total_chars >= MEMORY_FLUSH["force_flush_chars"]:
        return True
    return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]


def _render_discarded_text(discarded_messages: Sequence[Any]) -> str:
    return "\n\n".join(f"[{_msg_type(m)}] {_msg_to_text(m)}" for m in discarded_messages)


def _build_llm(llm_factory: Callable[..., Any]) -> Any:
    return llm_factory(
        model=MEMORY_FLUSH["model"] or None,
        max_tokens=MEMORY_FLUSH["output_max_tokens"],
        timeout=MEMORY_FLUSH["timeout_seconds"],
    )


async def run_memory_flush(
    discarded_messages: Sequence[Any],
    estimated_tokens: int,
    memory_store: Any,
    llm_factory: Callable[..., Any],
) -> bool:
    """Extract facts from discarded messages and append them to MEMORY.md.

    Returns True on a successful write, False when skipped, empty, or failed.
    Never raises: a flush failure must not block compression.
    """
    if not should_flush(discarded_messages, estimated_tokens):
        return False

    discarded_text = _render_discarded_text(discarded_messages)
    if not discarded_text.strip():
        return False

    prompt = _FLUSH_PROMPT.format(discarded_text=discarded_text)
    try:
        llm = _build_llm(llm_factory)
        response = await llm.ainvoke(prompt)
        extracted = _extract_text(response)

        if not extracted or extracted.strip() == "(none)":
            logger.info("Memory Flush: no facts to extract")
            return False

        result = memory_store.append_entries(extracted)
        logger.info(
            "Memory Flush: wrote %d chars to MEMORY.md (%s)",
            len(extracted),
            result.get("message", ""),
        )
        return True
    except Exception as e:  # noqa: BLE001 -- boundary: flush must never break compression
        logger.warning("Memory Flush failed (non-blocking): %s", e)
        return False


def run_memory_flush_sync(
    discarded_messages: Sequence[Any],
    estimated_tokens: int,
    memory_store: Any,
    llm_factory: Callable[..., Any],
) -> bool:
    """Synchronous variant of :func:`run_memory_flush` (sync compression path)."""
    if not should_flush(discarded_messages, estimated_tokens):
        return False

    discarded_text = _render_discarded_text(discarded_messages)
    if not discarded_text.strip():
        return False

    prompt = _FLUSH_PROMPT.format(discarded_text=discarded_text)
    try:
        llm = _build_llm(llm_factory)
        response = llm.invoke(prompt)
        extracted = _extract_text(response)

        if not extracted or extracted.strip() == "(none)":
            logger.info("Memory Flush: no facts to extract")
            return False

        result = memory_store.append_entries(extracted)
        logger.info(
            "Memory Flush: wrote %d chars to MEMORY.md (%s)",
            len(extracted),
            result.get("message", ""),
        )
        return True
    except Exception as e:  # noqa: BLE001 -- boundary: flush must never break compression
        logger.warning("Memory Flush failed (non-blocking): %s", e)
        return False


def _msg_to_text(msg: Any) -> str:
    """Flatten a message (or raw string) into plain text."""
    if isinstance(msg, str):
        return msg
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(msg)


def _msg_type(msg: Any) -> str:
    """Return a short role label for a message."""
    label = getattr(msg, "type", "")
    return str(label) if label else type(msg).__name__.lower()


def _extract_text(response: Any) -> str:
    """Pull the text payload out of a model response."""
    content = getattr(response, "content", None)
    if content is not None:
        return content.strip() if isinstance(content, str) else str(content).strip()
    text = getattr(response, "text", None)
    if text is not None:
        return str(text).strip()
    return str(response).strip()
