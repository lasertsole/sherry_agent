"""Read file tool with pagination support (offset + limit) and line numbers."""
import json
from typing import Optional, Type

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from tools.pub_base import resolve_path


class ReadFileInput(BaseModel):
    """Input schema for paginated read_file."""

    file_path: str = Field(
        description="Path to the file to read (absolute, relative, or ~/path)"
    )
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
    args_schema: Type[BaseModel] = ReadFileInput
    description: str = (
        "Read a file with pagination and line numbers. "
        "Use offset and limit to read specific sections of large files."
    )
    metadata: dict = {"idempotent": True}

    def _run(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 500,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        resolved = resolve_path(file_path)

        if not resolved.exists():
            return json.dumps({"error": f"File not found: {file_path}"}, ensure_ascii=False)
        if resolved.is_dir():
            return json.dumps({"error": f"Path is a directory: {file_path}"}, ensure_ascii=False)

        try:
            file_size = resolved.stat().st_size
        except OSError:
            file_size = 0

        try:
            with resolved.open("r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception as e:
            return json.dumps({"error": f"Failed to read file: {e}"}, ensure_ascii=False)

        if raw.startswith("\ufeff"):
            raw = raw[1:]

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


def build_read_file_tool() -> ReadFileTool:
    tool = ReadFileTool()
    tool.handle_tool_error = True
    return tool
