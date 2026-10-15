"""Read file tool with pagination support (offset + limit) and line numbers.

The window is bounded by BOTH a token budget (``read_file_token_budget``, 4000,
CJK-aware: CJK ~2 chars/token, ASCII ~4) and the caller's line ``limit`` — the
stricter wins. The budget is what makes the same response cost the same context
in every language: 2000 lines of English are ~20 000 tokens while 2000 lines of
Chinese are ~50 000. The truncation point is always a line boundary, and a
single line that alone blows the budget is clipped inside itself with a marker —
WHEREVER it sits in the page, so one minified line can never ride the response
whole. ``offset`` counts LINES, so read_file cannot page inside a clipped line:
the hint names the tools that can (search_files / terminal / python_repl).

Read-before-write contract: a COMPLETE read (the first page, nothing truncated)
licenses ``write_file`` to overwrite exactly this revision of the file — see
``agent/tools/pub_base/read_state.py``. The revision is taken from the open
descriptor's ``fstat``, so it describes the bytes actually read; a partial read
licenses nothing, and a file that changes afterwards makes the license stale,
which is what refuses the overwrite. A re-read of a file this session already
read IN FULL (the license exists) and that has not changed answers with a
pointer instead of the content — read a range (offset >= 2) to force content.
"""

import json
import os
from pathlib import Path

from loguru import logger
from typing import override
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from agent.tools.pub_base import SessionId
from agent.tools.pub_base import (
    tool_error,
    resolve_tool_session_id,
    _open_no_follow,
    decode_text,
    display_path,
    licensed,
    note_read,
    resolve_tool_path,
    revision_id,
    safe_error_detail,
    sniff_text_encoding,
)
from config.features import TOKEN_ESTIMATION, TOOLS_TIMEOUTS
from pub.func.cjk import is_cjk_codepoint
from pub.func.message.token_truncation import _chars_for_tokens
from langchain_core.callbacks import CallbackManagerForToolRun


#: Line-count window: the registry is the single source of truth (the schema
#: and the pager both used to hardcode 500/2000).
_READ_DEFAULT_LIMIT = TOOLS_TIMEOUTS["file_tools_read_default_limit"]
_READ_MAX_LIMIT = TOOLS_TIMEOUTS["file_tools_read_max_limit"]

#: Token budget for one response, and how much of it a page may spend. The
#: ratios mirror ``token_truncation`` (CJK ≈ 2 chars/token, ASCII ≈ 4), so the
#: same budget costs the same context in every language; a budget of 0 (or
#: less) restores the pure line-count window.
_TOKEN_BUDGET = TOOLS_TIMEOUTS["read_file_token_budget"]
_BUDGET_RATIO = TOOLS_TIMEOUTS["read_file_budget_ratio"]
#: HARD per-line ceiling: a line costing more than this is clipped wherever it
#: sits, whatever the page budget says — even with the budget switched off. One
#: minified line can therefore never ride the response whole.
_LINE_TOKEN_CAP = TOOLS_TIMEOUTS["read_file_max_line_tokens"]
_CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
_CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]

#: Marker put inside a single line that alone blows the whole budget (a
#: minified bundle): the head of that line comes back clipped, so the response
#: stays bounded instead of carrying one 500 MB "line" whole. Applies to every
#: line of the page, not only the first.
_LINE_CLIP_MARKER = "...[{omitted} chars clipped]..."

#: Answer for a re-read of a file this session already read in full and that
#: has not changed: the content is in the conversation, so the pointer is the
#: honest response. Read a RANGE (offset ≥ 2) to force content back.
_UNCHANGED_STUB = (
    "[File unchanged since your last read — its content is already in this "
    "conversation. Do not re-read the whole file; read a specific range with "
    "offset/limit if you need to re-check part of it.]"
)


def _line_token_cost(line: str) -> float:
    """One line's token cost, CJK-aware (the same ratios ``token_truncation`` uses)."""
    total = 0.0
    for char in line:
        total += (
            1.0 / _CHARS_PER_TOKEN_CJK if is_cjk_codepoint(ord(char)) else 1.0 / _CHARS_PER_TOKEN
        )
    return total


def _unchanged_since_last_read(
    session_id: str,
    resolved: Path,
    offset: int,
    read_stat: os.stat_result,
) -> bool:
    """True when this session already holds exactly these bytes in context.

    Only a from-the-start read can be stubbed (``offset == 1``) and only off a
    license — and a license exists only for a COMPLETE prior read, so a
    truncated or ranged first read never stubs. The revision comes from the
    descriptor whose bytes were just read, so the comparison describes exactly
    that content.
    """
    if offset != 1 or not session_id:
        return False
    license_entry = licensed(session_id, resolved)
    if license_entry is None or license_entry.source != "read":
        # No license, or a license this session got by WRITING the file: the
        # content is not something it read (and a caller asking to read a file
        # it just wrote gets the content, not a pointer).
        return False
    return license_entry.revision == revision_id(read_stat)


class ReadFileInput(BaseModel):
    """Input schema for paginated read_file."""

    file_path: str = Field(description="Path to the file to read (absolute, relative, or ~/path)")
    offset: int = Field(
        default=1,
        ge=1,
        description="Line number to start reading from (1-indexed, default: 1)",
    )
    limit: int = Field(
        default=_READ_DEFAULT_LIMIT,
        ge=1,
        le=_READ_MAX_LIMIT,
        description=(
            f"Maximum number of lines to read "
            f"(default: {_READ_DEFAULT_LIMIT}, max: {_READ_MAX_LIMIT}); "
            f"the response is also capped at ~{_TOKEN_BUDGET} tokens"
        ),
    )
    session_id: SessionId = ""


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    """Add line numbers in ``LINE_NUM|CONTENT`` format (compact, no padding)."""
    lines = content.split("\n")
    numbered = []
    for i, line in enumerate(lines, start=start_line):
        numbered.append(f"{i}|{line}")
    return "\n".join(numbered)


class ReadFileTool(BaseTool):
    """Read a file with pagination and line numbers.

    Returns JSON with:
      - content:      line-numbered text (``LINE_NUM|CONTENT`` format)
      - total_lines:  total lines in the file
      - file_size:    file size in bytes
      - truncated:    whether the response was truncated
      - hint:         (optional) pagination hint when truncated
    """

    name: str = "read_file"
    args_schema: type[BaseModel] = ReadFileInput
    description: str = (
        "Read a file with pagination and line numbers. "
        "Use offset and limit to read specific sections of large files. "
        "Reading the WHOLE file (first page, nothing truncated) is what allows "
        "write_file to overwrite it later — write_file refuses to replace a file "
        "this session has not read."
    )
    metadata: dict = {"idempotent": True}

    # ── shared core ────────────────────────────────────────────────────────

    def _read_bytes(self, resolved: Path) -> tuple[bytes | None, os.stat_result | None, str | None]:
        """Read every byte through a no-follow descriptor.

        The descriptor also yields the revision of exactly the bytes about to
        be read: a writer replacing the path mid-read cannot license content
        this call never saw.
        """
        try:
            fd = _open_no_follow(resolved, os.O_RDONLY)
            try:
                read_stat = os.fstat(fd)
                with os.fdopen(fd, "rb") as f:
                    fd = -1
                    data = f.read()
            finally:
                if fd >= 0:
                    os.close(fd)
        except Exception as e:
            logger.debug("read_file: read failed", exc_info=True)
            return None, None, tool_error(f"Failed to read file: {safe_error_detail(e)}")
        return data, read_stat, None

    def _core(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = _READ_DEFAULT_LIMIT,
        session_id: str = "",
    ) -> str:
        root, resolved, error = resolve_tool_path(file_path, session_id, "read file")
        if error is not None or resolved is None or root is None:
            return error or tool_error("Could not resolve the path")
        root = root

        if not resolved.exists():
            return json.dumps(
                {"error": f"File not found: {display_path(resolved, root)}"}, ensure_ascii=False
            )
        if resolved.is_dir():
            return json.dumps(
                {"error": f"Path is a directory: {display_path(resolved, root)}"},
                ensure_ascii=False,
            )

        try:
            file_size = resolved.stat().st_size
        except OSError:
            file_size = 0

        data, read_stat, error = self._read_bytes(resolved)
        if error is not None or data is None or read_stat is None:
            return error or tool_error("Failed to read file")

        return self._paginate(resolved, root, data, read_stat, file_size, offset, limit, session_id)

    def _paginate(
        self,
        resolved: Path,
        root: Path,
        data: bytes,
        read_stat: os.stat_result,
        file_size: int,
        offset: int,
        limit: int,
        session_id: str,
    ) -> str:
        """Decode a window of the file and license a whole-file read.

        A binary (or non-UTF-8/UTF-16) file is refused with its size and never
        licensed, so write_file cannot replace it with text.
        """
        encoding = sniff_text_encoding(data)
        if encoding is None:
            return json.dumps(
                {
                    "error": (
                        "File is binary or not UTF-8/UTF-16 text "
                        f"({file_size} bytes); read_file shows text only."
                    ),
                    "path": display_path(resolved, root),
                    "hint": (
                        "Use terminal (file/xxd/strings, or python) to inspect resource "
                        "files; write_file and patch_file do not edit them."
                    ),
                },
                ensure_ascii=False,
            )
        raw = decode_text(data, encoding)

        all_lines = raw.splitlines(keepends=True)
        total_lines = len(all_lines)
        offset = max(1, min(offset, total_lines + 1))
        limit = max(1, min(limit, _READ_MAX_LIMIT))

        if _unchanged_since_last_read(session_id, resolved, offset, read_stat):
            return json.dumps(
                {
                    "content": _UNCHANGED_STUB,
                    "total_lines": total_lines,
                    "file_size": file_size,
                    "truncated": False,
                    "unchanged": True,
                },
                ensure_ascii=False,
            )

        page_lines, spent_tokens, clipped = self._window(all_lines, offset, limit)

        end_line = offset + len(page_lines) - 1
        page_text = "".join(page_lines)

        if page_text.endswith("\n"):
            page_text = page_text[:-1]

        numbered_content = _add_line_numbers(page_text, offset) if page_text else ""
        # A clipped single line is truncation too: the caller has NOT seen the
        # whole file, so no write license may be granted off this read.
        truncated = total_lines > end_line or clipped

        if offset == 1 and not truncated:
            # The whole file is in this response: license an overwrite of this
            # revision (a partial read licenses nothing). The codec rides along,
            # so a later write keeps the file's own encoding.
            note_read(session_id, resolved, revision_id(read_stat), encoding=encoding)

        result = {
            "content": numbered_content,
            "total_lines": total_lines,
            "file_size": file_size,
            "truncated": truncated,
        }
        if truncated:
            shown_end = min(end_line, total_lines)
            hint = (
                f"Use offset={end_line + 1} to continue reading "
                f"(showing {offset}-{shown_end} of {total_lines} lines"
            )
            if clipped:
                # The one case read_file cannot page through itself: `offset`
                # counts LINES, so the rest of a clipped line has no read_file
                # address. Name the tools that do reach it.
                hint += (
                    "; one line alone exceeded the whole budget and was clipped"
                    " — read_file cannot page INSIDE a line, so for that line's"
                    " remainder use search_files (regex with context), or terminal"
                    " (head -c / tail -c / dd) / python_repl (slicing)"
                )
            if _TOKEN_BUDGET > 0:
                hint += f", ~{int(spent_tokens)} tokens of the {_TOKEN_BUDGET} budget"
            result["hint"] = hint + ")"

        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _window(all_lines: list[str], offset: int, limit: int) -> tuple[list[str], float, bool]:
        """The lines to return, bounded by BOTH the token budget and *limit*.

        The walk stops at a LINE boundary, so line numbers stay continuous (the
        window may overshoot the budget by the whole line that crossed it — that
        line is itself capped, see below, so the page stays within roughly twice
        the budget).

        PER-LINE HARD CAP: a line that alone blows the page budget is clipped
        inside itself with a marker — wherever it sits in the page, not just
        first. (Clipping only the page's first line left the real hole: a
        minified 500 MB "line" on line 2 rode the response WHOLE and pushed it
        past every bound.) The ceiling is ``read_file_max_line_tokens``, which
        applies even when the page budget is switched off. ``clipped`` reports
        that a clip happened, so the caller knows the file was NOT seen whole
        and no write license may ride this read. With the budget switched off
        (0) this is the line-count window plus that per-line ceiling.

        :returns: ``(page_lines, spent_tokens, clipped)``.
        """
        target_tokens = _TOKEN_BUDGET * _BUDGET_RATIO if _TOKEN_BUDGET > 0 else float("inf")
        # The per-line ceiling always applies; with the page budget on, the
        # stricter of the two decides (the budget, normally).
        per_line_cap = (
            min(target_tokens, float(_LINE_TOKEN_CAP)) if _LINE_TOKEN_CAP > 0 else target_tokens
        )
        page_lines: list[str] = []
        spent = 0.0
        clipped = False
        for index in range(offset - 1, len(all_lines)):
            if len(page_lines) >= limit:
                break
            line = all_lines[index]
            cost = _line_token_cost(line)
            if cost > per_line_cap:
                keep = _chars_for_tokens(line, per_line_cap)
                omitted = len(line) - keep
                page_lines.append(line[:keep] + _LINE_CLIP_MARKER.format(omitted=omitted))
                spent += per_line_cap
                clipped = True
                break
            page_lines.append(line)
            spent += cost
            if spent >= target_tokens:
                break
        return page_lines, spent, clipped

    @override
    def _run(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = _READ_DEFAULT_LIMIT,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = resolve_tool_session_id(session_id, run_manager)
        return self._core(file_path, offset, limit, session_id)

    @override
    async def _arun(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = _READ_DEFAULT_LIMIT,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = resolve_tool_session_id(session_id, run_manager)
        return self._core(file_path, offset, limit, session_id)


def build_read_file_tool() -> ReadFileTool:
    tool = ReadFileTool()
    tool.handle_tool_error = True
    return tool
