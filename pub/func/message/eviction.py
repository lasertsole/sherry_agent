"""Context eviction: offload oversized payloads, keep a recoverable preview.

P0-2 (execution-time eviction): the full text of an oversized ``ToolMessage``
is written under ``SESSIONS_DIR/{session_id}/evicted/`` and the content is
replaced by a head/tail preview carrying the file path — the big payload never
reaches graph state. The model recovers the rest with
``read_file(file_path=..., offset=..., limit=...)``.

P1-9 (human-message eviction): an oversized plain-text ``HumanMessage`` is
written to the same directory and tagged in ``additional_kwargs`` with
``lc_evicted_to``; its content and id stay untouched, so graph state and
MesMemory keep the full text and only the model view is truncated (see
``agent/middlewares/context_eviction/core.py``).

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
import json
import re
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from loguru import logger

from config import SESSIONS_DIR
from config.features.agent_side.tool_result_eviction import TOOL_RESULT_EVICTION
from config.path import is_safe_session_segment

__all__ = [
    "EVICTED_TO_KEY",
    "build_file_preview",
    "build_human_preview",
    "build_preview",
    "evict_human_message",
    "evict_tool_result",
    "get_eviction_dir",
    "human_eviction_notice",
    "load_evicted",
    "slice_read_file_result",
]

# additional_kwargs marker shared by both eviction paths: its value is the
# path of the file holding the full text.
EVICTED_TO_KEY = "lc_evicted_to"

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

Use read_file(file_path='{path}', offset=0, limit=100) to read the full content in chunks.{cut_note}]"""

#: Same shape as ``_PREVIEW_TEMPLATE``, but for a payload that is ALREADY a file
#: (the terminal tool's fd-direct-write stdout): the total is the file's byte
#: size — an O(1) ``stat`` — because counting characters would mean decoding the
#: whole file, which is exactly what the fd path exists to avoid.
_FILE_PREVIEW_TEMPLATE = """\
[evicted to: {path}]
--- head ({head_n} lines) ---
{head}
{midline}
--- tail ({tail_n} lines) ---
{tail}
[full content: {total_bytes} bytes, evicted at {ts}]

Use read_file(file_path='{path}', offset=0, limit=100) to read the full content in chunks.{cut_note}]"""

_HUMAN_PREVIEW_TEMPLATE = """\
[evicted to: {path}]
--- head ({head_n} lines) ---
{head}
{midline}
--- tail ({tail_n} lines) ---
{tail}
[full content: {total_chars} chars, evicted at {ts}]

{notice}"""

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


#: CHAR budgets shared by BOTH preview renderers — the exact mirror of the
#: ``_PREVIEW_*_BYTES`` set the file-on-disk preview uses: each kept line is
#: capped, each side stops at its budget, so a newline-free blob previews to a
#: few KB whatever the payload's shape. These are a HARD cap: the plain line
#: window applies them too, because preferring "whole lines" without them let a
#: payload that shrinks by one line still hand back a near-original-size preview
#: with one giant line (or one giant line plus a few) riding it whole.
_PREVIEW_HEAD_MAX_CHARS = 8_192
_PREVIEW_LINE_MAX_CHARS = 2_048
_PREVIEW_TAIL_MAX_CHARS = 8_192


def _take_preview_side(lines: list[str], limit_n: int, budget: int) -> tuple[list[str], bool, bool]:
    """Keep up to *limit_n* lines within *budget* chars, capping each line.

    :returns: ``(kept, dropped, clipped)`` — ``dropped`` is True when a line was
        left out or clipped, which decides the ``...`` separator; ``clipped`` is
        True only for a line cut IN THE MIDDLE (what the cut note reports).
    """
    kept: list[str] = []
    used = 0
    dropped = False
    clipped = False
    for line in lines:
        if len(kept) >= limit_n or used >= budget:
            dropped = True
            break
        if len(line) > _PREVIEW_LINE_MAX_CHARS:
            line = line[:_PREVIEW_LINE_MAX_CHARS] + "\u2026"
            dropped = True
            clipped = True
        kept.append(line)
        used += len(line)
    return kept, dropped, clipped


def _render_head_tail(
    content: str, head_n: int, tail_n: int
) -> tuple[str, str, str, int, int, bool]:
    """The head/tail line window, with the per-line and per-side CHAR budgets.

    The line COUNT shapes the window (whole lines read like the payload) and the
    budgets bound its cost: every line is capped at ``_PREVIEW_LINE_MAX_CHARS``
    and each side stops at its own budget, so the preview is a few KB whatever
    the payload looks like — one giant line included.

    A payload with ``head_n + tail_n`` lines or fewer has overlapping windows
    (the head and the tail show the same lines), so the render can be LARGER
    than the payload; ``_build_preview`` then hands back this render and the
    caller's ``len(preview) >= len(text)`` net keeps the original message.

    :returns: ``(head, midline, tail, head_shown, tail_shown, clipped)`` —
        ``clipped`` is True when a shown line was cut in the middle.
    """
    if not content:
        return "", "", "", 0, 0, False
    lines = content.splitlines()
    head_lines, head_dropped, head_clipped = _take_preview_side(
        lines, head_n, _PREVIEW_HEAD_MAX_CHARS
    )
    tail_lines, tail_dropped, tail_clipped = _take_preview_side(
        list(reversed(lines)), tail_n, _PREVIEW_TAIL_MAX_CHARS
    )
    midline = "..." if (head_dropped or tail_dropped) else ""
    return (
        "\n".join(head_lines),
        midline,
        "\n".join(reversed(tail_lines)),
        len(head_lines),
        len(tail_lines),
        head_clipped or tail_clipped,
    )


def _build_preview(template: str, content: str, head_n: int, tail_n: int, **fields) -> str:
    """Render the head/tail preview of *content* (per-line and per-side capped).

    The caps make the preview a few KB whatever the payload's shape, so a
    minified single line cannot ride it whole. When the render is not smaller
    than the payload (the overlapping-window shape) the caller's own
    ``len(preview) >= len(text)`` net keeps the original message.
    """
    head, midline, tail, head_shown, tail_shown, clipped = _render_head_tail(
        content, head_n, tail_n
    )
    cut_note = "\n\n" + _MIDLINE_CUT_NOTE if clipped else ""
    return template.format(
        head=head,
        midline=midline,
        tail=tail,
        head_n=head_shown,
        tail_n=tail_shown,
        cut_note=cut_note,
        **fields,
    )


def build_preview(content: str, file_path: Path) -> str:
    """Render the head/tail preview that replaces an evicted result."""
    return _build_preview(
        _PREVIEW_TEMPLATE,
        content,
        TOOL_RESULT_EVICTION["preview_head_lines"],
        TOOL_RESULT_EVICTION["preview_tail_lines"],
        path=str(file_path),
        total_chars=len(content),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def human_eviction_notice(file_path: str | Path) -> str:
    """Model-facing recovery notice for an evicted human message."""
    return (
        f"This user message exceeded the eviction threshold; its full text is at "
        f"'{file_path}'. Use read_file(file_path='{file_path}', offset=0, limit=100) "
        f"to read the full content in chunks. If it is one very long line, read_file "
        f"shows only its head — use terminal (head -c / tail -c) or python_repl "
        f"(slicing) for the rest."
    )


def build_human_preview(content: str, file_path: Path) -> str:
    """Render the head/tail preview that replaces an evicted human message."""
    return _build_preview(
        _HUMAN_PREVIEW_TEMPLATE,
        content,
        TOOL_RESULT_EVICTION["human_preview_head_lines"],
        TOOL_RESULT_EVICTION["human_preview_tail_lines"],
        path=str(file_path),
        total_chars=len(content),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
        notice=human_eviction_notice(file_path),
    )


def _human_file_name(msg: HumanMessage) -> str:
    msg_id = getattr(msg, "id", None)
    if isinstance(msg_id, str) and msg_id:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", msg_id).strip("._-")[:64]
        if safe:
            return f"human-{safe}.md"
    return f"human-{time.time_ns()}.md"


def evict_human_message(msg: HumanMessage, session_id: str) -> HumanMessage | None:
    """Offload an oversized human message; return the tagged copy or ``None``.

    ``None`` means "keep the original": human eviction is disabled, the text is
    at or under ``human_evict_threshold_chars``, the message already carries
    ``lc_evicted_to``, even the character-budgeted preview would not be smaller
    than the original (see :func:`_build_preview` — with the capped budgets this
    only trips for a payload barely over the threshold), the session id is
    unsafe, or the file write failed.

    The returned copy keeps the **content and id unchanged** and only adds
    ``additional_kwargs["lc_evicted_to"]``. The graph's ``add_messages``
    reducer therefore updates the message in place (no ``REMOVE_ALL_MESSAGES``
    rewrite), and graph state / checkpoint / MesMemory all keep the full text —
    only the model view is truncated later, by ``wrap_model_call``. The file is
    written before the tag is produced, so a failed write never yields a tag
    pointing at a missing file.
    """
    if not TOOL_RESULT_EVICTION["human_evict_enabled"]:
        return None
    if msg.additional_kwargs.get(EVICTED_TO_KEY):
        return None
    text = _extract_text(msg.content)
    if len(text) <= TOOL_RESULT_EVICTION["human_evict_threshold_chars"]:
        return None
    eviction_dir = _eviction_dir_path(session_id)
    if eviction_dir is None:
        return None
    file_path = eviction_dir / _human_file_name(msg)
    preview = build_human_preview(text, file_path)
    if len(preview) >= len(text):
        return None
    eviction_dir.mkdir(parents=True, exist_ok=True)
    try:
        file_path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    additional_kwargs = {**msg.additional_kwargs, EVICTED_TO_KEY: str(file_path)}
    return msg.model_copy(update={"additional_kwargs": additional_kwargs})


def evict_tool_result(msg: ToolMessage, session_id: str) -> ToolMessage | None:
    """Offload an oversized tool result; return the preview message or ``None``.

    ``None`` means "keep the original": the tool is excluded, the result is at
    or under the threshold, the session id is unsafe, the message is already a
    preview, or even the character-budgeted preview would not be smaller than
    the original (see :func:`_build_preview` — with the capped budgets this only
    trips for a payload barely over the threshold).

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
    key = f"{tool_call_id}_{hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:8]}"
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


def _read_file_is_self_truncated(content: str) -> bool:
    """True when a ``read_file`` payload declares its own truncation.

    ``read_file`` budgets every page in tokens (``read_file_token_budget``) and
    its own continuation hint is what the model needs; slicing such a page again
    would cut away content the tool deliberately kept (a CJK page at the budget
    is ~9 300 characters — the 4 000-character slice discarded more than half of
    it) and duplicate the recovery notice. A payload that does not parse, or one
    that says ``truncated: false``, still takes the slice.
    """
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("truncated") is True


def slice_read_file_result(msg: ToolMessage) -> ToolMessage:
    """Slice a ``read_file`` result in place of offloading (P2-4).

    The file is still on disk, so no eviction file is written: the content
    becomes the first ``_READ_FILE_SLICE_CHARS`` characters plus a recovery
    notice pointing the model back at ``read_file(offset, limit)``. A page the
    tool already budgeted itself (``truncated: true``) is returned untouched —
    the tool's own window is the intended size, and its hint is the recovery
    path. Idempotent: an already-sliced result (notice present) is untouched.
    """
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    if (
        len(content) <= _READ_FILE_SLICE_CHARS
        or _READ_FILE_SLICE_NOTICE in content
        or _read_file_is_self_truncated(content)
    ):
        return msg
    return msg.model_copy(
        update={"content": content[:_READ_FILE_SLICE_CHARS] + _READ_FILE_SLICE_NOTICE}
    )


#: Byte budgets for :func:`_read_head_tail`. Lines bound the shape, these bound
#: the cost: an output with NO newline (one giant line) would otherwise be read
#: into memory whole — the exact payload the file-on-disk preview exists to keep
#: out of the process. Every line is read with ``readline(cap)``, so a single
#: 500 MB line contributes at most ``_PREVIEW_LINE_MAX_BYTES`` per head slot,
#: the head stops at ``_PREVIEW_HEAD_MAX_BYTES`` and the tail walk stops at
#: ``_PREVIEW_TAIL_MAX_BYTES``.
_PREVIEW_HEAD_MAX_BYTES = 8_192
_PREVIEW_LINE_MAX_BYTES = 2_048
_PREVIEW_TAIL_MAX_BYTES = 8_192


def _read_head_tail(path: Path, head_n: int, tail_n: int) -> tuple[str, str, str, int, bool]:
    """Head/tail text of a file ON DISK, without loading it whole.

    The head is read line by line — each ``readline`` capped, the walk stopped
    at a byte budget — and the tail is read backwards in 8 KiB blocks until
    ``tail_n`` lines are collected or the byte budget is spent. Memory is
    bounded by those chunks whatever the file's size, so a 500 MB output (even
    a single newline-free line) costs the same as a 500 KB one.

    A line longer than ``_PREVIEW_LINE_MAX_BYTES`` is cut silently by that cap
    (the chunk simply stops), and the tail window's first line is a fragment
    whenever the backwards walk did not reach byte 0 — both are reported through
    the returned ``cut`` flag so the preview can say so instead of letting a
    mid-line cut read as a line ending.

    :returns: ``(head, midline, tail, head_lines_kept, cut)`` — ``midline`` is
        ``"..."`` when the preview window did not reach the end of the file,
        ``cut`` is True when a shown line was cut in the middle.
    """
    head_lines: list[str] = []
    head_budget = _PREVIEW_HEAD_MAX_BYTES
    cut = False
    try:
        with path.open("rb") as f:
            while len(head_lines) < head_n and head_budget > 0:
                cap = min(_PREVIEW_LINE_MAX_BYTES, head_budget)
                chunk = f.readline(cap)
                if not chunk:
                    break
                head_budget -= len(chunk)
                head_lines.append(chunk.decode("utf-8", errors="replace").rstrip("\n"))
                if len(chunk) >= cap and not chunk.endswith(b"\n"):
                    # The line did not fit the cap… unless it ended exactly AT
                    # it: the very next byte tells the two apart.
                    if f.peek(1)[:1] != b"\n":
                        cut = True
                        break
    except OSError:
        logger.debug("eviction: preview source unreadable at its head", exc_info=True)

    tail_lines: list[str] = []
    buf = b""
    pos = 0
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            while pos > 0 and len(tail_lines) < tail_n and len(buf) < _PREVIEW_TAIL_MAX_BYTES:
                read_size = min(8192, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
                tail_lines = buf.decode("utf-8", errors="replace").splitlines()
            if pos > 0:
                # The backwards walk stopped inside the file, so the FIRST line
                # of the tail buffer is a fragment (the byte before `pos` is not
                # a newline — otherwise the buffer starts on a line boundary).
                f.seek(pos - 1)
                if f.read(1) != b"\n":
                    cut = True
    except OSError:
        logger.debug("eviction: preview source unreadable at its tail", exc_info=True)

    head = "\n".join(head_lines)
    tail = "\n".join(tail_lines[-tail_n:])[-_PREVIEW_TAIL_MAX_BYTES:]
    more = len(head_lines) >= head_n or (pos > 0 and len(tail_lines) >= tail_n)
    midline = "..." if more else ""
    return head, midline, tail, len(head_lines), cut


#: Appended to a preview whose shown head/tail cut a line in the middle. The
#: cut itself is invisible (a clipped line looks like a normal line ending), and
#: the recovery advice differs from the normal one: ``read_file`` pages by LINE
#: and cannot page inside one.
_MIDLINE_CUT_NOTE = (
    "One very long line was cut above (it does not fit the per-line preview "
    "budget). `read_file` pages by line number and CANNOT read the rest of that "
    "line — use `search_files` (regex with context), or `terminal` "
    "(`head -c` / `tail -c` / `dd`) / `python_repl` (slicing) to reach it."
)


def build_file_preview(source: Path, file_path: Path) -> str:
    """Render the head/tail preview for a payload that already lives on disk.

    ``source`` is the file to preview (the fd-direct-written output);
    ``file_path`` is the path the model is told to read back (normally the same
    file once it has been moved into the eviction directory).
    """
    head_n = TOOL_RESULT_EVICTION["preview_head_lines"]
    tail_n = TOOL_RESULT_EVICTION["preview_tail_lines"]
    head, midline, tail, _kept, cut = _read_head_tail(source, head_n, tail_n)
    try:
        total_bytes = source.stat().st_size
    except OSError:
        total_bytes = 0
    return _FILE_PREVIEW_TEMPLATE.format(
        path=str(file_path),
        head_n=head_n,
        tail_n=tail_n,
        head=head,
        midline=midline,
        tail=tail,
        total_bytes=total_bytes,
        cut_note=f"\n\n{_MIDLINE_CUT_NOTE}" if cut else "",
        ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
