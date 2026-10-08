"""Targeted tool-output truncation (head+tail keep with a recoverable marker).

Slicing an oversized ``ToolMessage`` keeps ``CONTENT_HEAD_RATIO`` of
``max_output_chars`` at the head and ``CONTENT_TAIL_RATIO`` at the tail, and
collapses the middle. ``read_file`` results are special-cased: the full file
is still on disk at the same path, so the middle is replaced with a recovery
notice carrying the original ``file_path`` and a 1-based continuation offset
instead of the anonymous omission marker every other tool gets.
"""

import json
import re

from config.features import SUMMARIZATION, TOOLS_TIMEOUTS
from langchain_core.messages import BaseMessage, ToolMessage, AIMessage, ToolCall
from pub.func.estimate_tokens import estimate_text_tokens

CONTENT_HEAD_RATIO = SUMMARIZATION["content_head_ratio"]
CONTENT_TAIL_RATIO = SUMMARIZATION["content_tail_ratio"]
MAX_TOOL_OUTPUT_CHARS = SUMMARIZATION["max_tool_output_chars"]
MIN_OUTPUT_CHARS_TO_TRUNCATE = SUMMARIZATION["min_output_chars_to_truncate"]

_READ_FILE_CONTINUE_LIMIT = TOOLS_TIMEOUTS["file_tools_read_default_limit"]

_OMISSION_TEMPLATE = "...[truncated {omitted} chars]..."
_READ_FILE_CONTINUE_NOTICE = (
    "...[truncated {omitted} chars of read_file output; the file is unchanged on disk. "
    "Use offset={offset} to continue reading: "
    "read_file(file_path='{path}', offset={offset}, limit={limit}).]..."
)
_READ_FILE_RESTART_NOTICE = (
    "...[truncated {omitted} chars of read_file output; the file is unchanged on disk. "
    "Re-read it from the start in chunks. "
    "Use offset=1 to continue reading: "
    "read_file(file_path='{path}', offset=1, limit={limit}).]..."
)

# ``json.dumps`` escapes these into two characters; every other control char
# becomes six (``\uXXXX``). ``ensure_ascii=False`` keeps the rest literal.
_JSON_TWO_CHAR_ESCAPES = frozenset('"\\\n\r\t\b\f')


def _truncate_content(
    content: str,
    max_chars: int,
    head_ratio: float = CONTENT_HEAD_RATIO,
    tail_ratio: float = CONTENT_TAIL_RATIO,
) -> str:
    if len(content) <= max_chars:
        return content
    head = content[: int(max_chars * head_ratio)]
    tail = content[-int(max_chars * tail_ratio) :]
    omitted = len(content) - len(head) - len(tail)
    return f"{head}{_OMISSION_TEMPLATE.format(omitted=omitted)}{tail}"


def _find_tool_call(messages: list[BaseMessage], target_idx: int, tc_id: str) -> ToolCall | None:
    """Return the tool_call matching ``tc_id``, searching backwards from ``target_idx``."""
    if not tc_id:
        return None
    for i in range(target_idx - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("id") == tc_id:
                    return tc
    return None


def _read_file_arg(tool_call: ToolCall | None) -> str | None:
    """Return ``read_file``'s ``file_path`` argument, or ``None`` for any other tool."""
    if tool_call is None or tool_call.get("name") != "read_file":
        return None
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return None
    path = args.get("file_path")
    return path if isinstance(path, str) and path else None


def _json_escaped_width(ch: str) -> int:
    if ch in _JSON_TWO_CHAR_ESCAPES:
        return 2
    return 6 if ord(ch) < 0x20 else 1


def _read_file_next_offset(content: str, head_chars: int) -> int | None:
    """1-based line to resume at after keeping only ``content[:head_chars]``.

    Locates the line-numbered ``content`` field inside the serialized
    read_file JSON and counts the line separators fully contained in the
    head: resuming at ``first_line + separators`` never skips a line that is
    only partially retained. Returns ``None`` when ``content`` is not a
    standard read_file JSON payload, so callers fall back to a conservative
    restart notice instead of guessing an offset.
    """
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("content")
    if not isinstance(inner, str) or not inner:
        return None
    first = re.match(r"(\d+)\|", inner)
    if first is None:
        return None
    escaped_body = json.dumps(inner, ensure_ascii=False)[1:-1]
    pos = content.find(escaped_body)
    if pos < 0:
        return None
    line_breaks = 0
    for ch in inner:
        width = _json_escaped_width(ch)
        if pos + width > head_chars:
            break
        pos += width
        if ch == "\n":
            line_breaks += 1
    return int(first.group(1)) + line_breaks


def _truncate_read_file_content(content: str, max_chars: int, file_path: str) -> str:
    """Head+tail clip a read_file result, middle replaced by a recovery notice."""
    head = content[: int(max_chars * CONTENT_HEAD_RATIO)]
    tail = content[-int(max_chars * CONTENT_TAIL_RATIO) :]
    omitted = len(content) - len(head) - len(tail)
    next_offset = _read_file_next_offset(content, len(head))
    if next_offset is None:
        notice = _READ_FILE_RESTART_NOTICE.format(
            omitted=omitted, path=file_path, limit=_READ_FILE_CONTINUE_LIMIT
        )
    else:
        notice = _READ_FILE_CONTINUE_NOTICE.format(
            omitted=omitted,
            path=file_path,
            offset=next_offset,
            limit=_READ_FILE_CONTINUE_LIMIT,
        )
    return f"{head}{notice}{tail}"


def target_truncate_tool_outputs(
    messages: list[BaseMessage],
    target_reduction_tokens: int,
    min_output_chars: int = MIN_OUTPUT_CHARS_TO_TRUNCATE,
    max_output_chars: int = MAX_TOOL_OUTPUT_CHARS,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    protected = protected_tools or set()

    candidates: list[tuple[int, str, ToolCall | None]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        content = str(getattr(msg, "content", ""))
        if len(content) < min_output_chars:
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        tool_call = _find_tool_call(messages, i, tc_id)
        tool_name = tool_call.get("name", "") if tool_call else ""
        if tool_name in protected:
            continue
        candidates.append((i, content, tool_call))

    candidates.sort(key=lambda x: len(x[1]), reverse=True)

    if not candidates:
        return messages, 0

    result = list(messages)
    total_reduced = 0

    for idx, content, tool_call in candidates:
        if total_reduced >= target_reduction_tokens:
            break
        file_path = _read_file_arg(tool_call)
        if file_path is not None and len(content) > max_output_chars:
            truncated = _truncate_read_file_content(content, max_output_chars, file_path)
        else:
            truncated = _truncate_content(content, max_output_chars)
        reduced_tokens = estimate_text_tokens(content) - estimate_text_tokens(truncated)
        total_reduced += max(reduced_tokens, 0)
        result[idx] = result[idx].model_copy(update={"content": truncated})

    return result, total_reduced
