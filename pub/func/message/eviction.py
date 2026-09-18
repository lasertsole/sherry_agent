"""Tool-result message eviction: offload oversized results, keep a preview.

P0-2 (execution-time eviction): the full text of an oversized ``ToolMessage``
is written under ``SESSIONS_DIR/{session_id}/evicted/`` and the content is
replaced by a head/tail preview carrying the file path — the big payload never
reaches graph state. The model recovers the rest with
``read_file(file_path=..., offset=..., limit=...)``.

P2-4 (execution-time read_file slicing): ``read_file`` results are NOT
offloaded — the file already lives on disk — so they are sliced to a fixed
head plus a recovery notice instead.

Complementarity with ``pub/func/message/target_truncation.py``: that module
covers the *compression-time* clip (head 30 % + tail 30 % of
``max_tool_output_chars``, with a parser-derived 1-based continuation offset).
This module covers the *tool-execution-time* pass. Both are staged reductions:
when compression later clips an already-sliced ``read_file`` payload, the
truncated JSON no longer parses, so the compression notice deterministically
falls back to "re-read from the start" — no incorrect offset is ever emitted.
The slice helper is idempotent, so the execution-time path never re-slices its
own output.

All session ids are validated as a single safe path segment before any write;
an unsafe id (empty, ``.`` / ``..``, or containing a separator) skips eviction.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage

from config import SESSIONS_DIR
from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION
from config.path import is_safe_session_segment

__all__ = [
    "build_preview",
    "evict_tool_result",
    "get_eviction_dir",
    "load_evicted",
    "slice_read_file_result",
]

# First line of every preview; also the idempotency marker (a second pass over
# an already-evicted message must be a no-op). EVICTION_PREFIX is the public
# alias shared with the P1-2 overflow tail clip, which must carry this pointer
# into its stub instead of destroying it.
_EVICTION_PREFIX = "[evicted to: "
EVICTION_PREFIX = _EVICTION_PREFIX

_PREVIEW_TEMPLATE = """\
[evicted to: {path}]
--- head ({head_n} lines) ---
{head}
{midline}
--- tail ({tail_n} lines) ---
{tail}
[full content: {total_chars} chars, evicted at {ts}]

Use read_file(file_path='{path}', offset=0, limit=100) to read the full content in chunks.]"""

# Execution-time read_file slice size (P2-4). The compression-time pass uses
# SUMMARIZATION["max_tool_output_chars"] instead; see the module docstring.
_READ_FILE_SLICE_CHARS = 4_000

_READ_FILE_SLICE_NOTICE = (
    "\n\n[Output was truncated due to eviction threshold. "
    "Use read_file with offset and limit to retrieve specific portions.]"
)
# Public alias: the P1-2 overflow tail clip re-emits this marker verbatim so a
# later slice pass still recognises an already-sliced read_file result.
READ_FILE_SLICE_NOTICE = _READ_FILE_SLICE_NOTICE


def _eviction_dir_path(session_id: str) -> Path | None:
    if not is_safe_session_segment(session_id):
        return None
    return Path(SESSIONS_DIR) / session_id / TOOL_RESULT_EVICTION["eviction_subdir"]


def get_eviction_dir(session_id: str) -> Path | None:
    """Return the session's eviction directory, creating it on demand.

    ``None`` when ``session_id`` is not a safe single path segment — the
    caller must skip eviction rather than write outside ``SESSIONS_DIR``.
    """
    directory = _eviction_dir_path(session_id)
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _extract_text(content: str | list[Any]) -> str:
    """Return the concatenated text of a message content payload.

    Multimodal blocks (``{"type": "image_url", ...}`` and friends) are not
    text and stay out of the evicted file — they are preserved in place by
    :func:`_build_evicted_content`.
    """
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


def _build_evicted_content(content: str | list[Any], preview: str) -> str | list[Any]:
    """Replace only the text portion of *content* with *preview*.

    Non-text blocks (images / audio / video) are kept verbatim and in order;
    the preview becomes the single leading text block.
    """
    if isinstance(content, str):
        return preview
    non_text = [block for block in content if not _is_text_block(block)]
    return [{"type": "text", "text": preview}, *non_text]


def build_preview(content: str, file_path: Path) -> str:
    """Render the head/tail preview that replaces an evicted result."""
    lines = content.splitlines()
    head_n = TOOL_RESULT_EVICTION["preview_head_lines"]
    tail_n = TOOL_RESULT_EVICTION["preview_tail_lines"]
    head = "\n".join(lines[:head_n])
    tail = "\n".join(lines[-tail_n:])
    midline = "..." if len(lines) > head_n + tail_n else ""
    return _PREVIEW_TEMPLATE.format(
        path=str(file_path),
        head_n=min(head_n, len(lines)),
        tail_n=min(tail_n, len(lines)),
        head=head,
        midline=midline,
        tail=tail,
        total_chars=len(content),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def evict_tool_result(msg: ToolMessage, session_id: str) -> ToolMessage | None:
    """Offload an oversized tool result; return the preview message or ``None``.

    ``None`` means "keep the original": the tool is excluded, the result is at
    or under the threshold, the session id is unsafe, the message is already a
    preview, or the preview would not be smaller than the original (a single
    huge line, where head and tail both contain the whole payload).

    The replacement is produced with :meth:`ToolMessage.model_copy`, so the
    LangGraph message id and ``additional_kwargs`` (including the in-process
    persistence marker) survive — the message-persistence watermark must keep
    seeing this logical message as already flushed.
    """
    if not TOOL_RESULT_EVICTION["enabled"]:
        return None
    if (getattr(msg, "name", "") or "") in TOOL_RESULT_EVICTION["excluded_tools"]:
        return None

    text = _extract_text(msg.content)
    if len(text) <= TOOL_RESULT_EVICTION["evict_threshold_chars"]:
        return None
    if text.startswith(_EVICTION_PREFIX):
        return None

    eviction_dir = _eviction_dir_path(session_id)
    if eviction_dir is None:
        return None

    tool_call_id = getattr(msg, "tool_call_id", "") or "unknown"
    key = f"{tool_call_id}_{hashlib.md5(text.encode()).hexdigest()[:8]}"
    file_path = eviction_dir / f"{key}.txt"

    preview = build_preview(text, file_path)
    if len(preview) >= len(text):
        return None

    eviction_dir.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text, encoding="utf-8")
    return msg.model_copy(update={"content": _build_evicted_content(msg.content, preview)})


def load_evicted(file_path: str | Path) -> str | None:
    """Read back an evicted file's full content (``None`` when unreadable)."""
    try:
        return Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return None


def slice_read_file_result(msg: ToolMessage) -> ToolMessage:
    """Slice a ``read_file`` result in place of offloading (P2-4).

    The file is still on disk, so no eviction file is written: the content
    becomes the first ``_READ_FILE_SLICE_CHARS`` characters plus a recovery
    notice pointing the model back at ``read_file(offset, limit)``. Idempotent
    — an already-sliced result (notice present) is returned untouched.
    """
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    if len(content) <= _READ_FILE_SLICE_CHARS or _READ_FILE_SLICE_NOTICE in content:
        return msg
    return msg.model_copy(
        update={"content": content[:_READ_FILE_SLICE_CHARS] + _READ_FILE_SLICE_NOTICE}
    )
