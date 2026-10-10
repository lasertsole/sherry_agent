"""Read file tool with pagination support (offset + limit) and line numbers.

Read-before-write contract: a COMPLETE read (the first page, nothing truncated)
licenses ``write_file`` to overwrite exactly this revision of the file — see
``agent/tools/pub_base/read_state.py``. The revision is taken from the open
descriptor's ``fstat``, so it describes the bytes actually read; a partial read
licenses nothing, and a file that changes afterwards makes the license stale,
which is what refuses the overwrite.
"""

import json
import os
from typing import override
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from agent.tools.pub_base import SessionId
from agent.tools.pub_base import (
    tool_error,
    PathOutOfBoundsError,
    _extract_session_id,
    _open_no_follow,
    decode_text,
    display_path,
    note_read,
    resolve_external_path,
    resolve_workspace_path,
    revision_id,
    safe_error_detail,
    sniff_text_encoding,
)
from langchain_core.callbacks import CallbackManagerForToolRun
from runtime.session.project_dir import current_project_dir


class ReadFileInput(BaseModel):
    """Input schema for paginated read_file."""

    file_path: str = Field(description="Path to the file to read (absolute, relative, or ~/path)")
    offset: int = Field(
        default=1,
        ge=1,
        description="Line number to start reading from (1-indexed, default: 1)",
    )
    limit: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Maximum number of lines to read (default: 500, max: 2000)",
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

    def _core(self, file_path: str, offset: int = 1, limit: int = 500, session_id: str = "") -> str:
        # redundant: path_guard middleware handles this — kept as the second line of defense
        try:
            root = current_project_dir(session_id)
            resolved = resolve_workspace_path(file_path, root)
        except PathOutOfBoundsError:
            try:
                resolved = resolve_external_path(
                    file_path, session_id=session_id, action_desc="read file"
                )
            except PathOutOfBoundsError as e:
                return tool_error(str(e))

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

        try:
            fd = _open_no_follow(resolved, os.O_RDONLY)
            try:
                # The revision of exactly the bytes about to be read: same
                # descriptor, so a writer replacing the path mid-read cannot
                # license content this call never saw.
                read_stat = os.fstat(fd)
                with os.fdopen(fd, "rb") as f:
                    fd = -1
                    data = f.read()
            finally:
                if fd >= 0:
                    os.close(fd)
        except Exception as e:
            return tool_error(f"Failed to read file: {safe_error_detail(e)}")

        encoding = sniff_text_encoding(data)
        if encoding is None:
            # A resource file (image, archive) or text in a codec we do not
            # edit: refuse with its size instead of returning mojibake — and
            # never license it, so write_file cannot replace it with text.
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
        limit = max(1, min(limit, 2000))

        end_line = offset + limit - 1
        page_lines = all_lines[offset - 1 : end_line]
        page_text = "".join(page_lines)

        if page_text.endswith("\n"):
            page_text = page_text[:-1]

        numbered_content = _add_line_numbers(page_text, offset) if page_text else ""
        truncated = total_lines > end_line

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
            result["hint"] = (
                f"Use offset={end_line + 1} to continue reading "
                f"(showing {offset}-{shown_end} of {total_lines} lines)"
            )

        return json.dumps(result, ensure_ascii=False)

    @override
    def _run(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 500,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(file_path, offset, limit, session_id)

    @override
    async def _arun(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 500,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(file_path, offset, limit, session_id)


def build_read_file_tool() -> ReadFileTool:
    tool = ReadFileTool()
    tool.handle_tool_error = True
    return tool
