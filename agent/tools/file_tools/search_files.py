"""Search files tool — pure Python, cross-platform.

Supports two search targets:
  - ``content``: grep-like regex search inside files (returns matches with line numbers)
  - ``files``: glob-like filename search (returns matching file paths)

No external dependencies (rg, grep, find) required.
"""
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Optional, Type

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from agent.tools.pub_base import is_text_file, resolve_path, should_skip_dir


# ── Content search (grep-like) ───────────────────────────────────────────

def _search_content(
    pattern: str, root: Path, file_glob: str | None,
    limit: int, offset: int, context: int,
) -> dict:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Invalid regex pattern: {e}"}

    matches: list[dict] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(Path(dirpath) / d)]

        for fname in sorted(filenames):
            if file_glob and not fnmatch.fnmatch(fname, file_glob):
                continue

            fpath = Path(dirpath) / fname
            if not fpath.is_file() or not is_text_file(fpath):
                continue

            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, PermissionError):
                continue

            for i, line in enumerate(lines):
                if regex.search(line):
                    ctx_before = lines[max(0, i - context) : i] if context else []
                    ctx_after = lines[i + 1 : i + 1 + context] if context else []
                    matches.append({
                        "path": str(fpath),
                        "line_number": i + 1,
                        "content": line[:500],
                        "context_before": ctx_before,
                        "context_after": ctx_after,
                    })
                    if len(matches) >= offset + limit + 1:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    total = len(matches)
    page = matches[offset : offset + limit]

    result_matches = []
    for m in page:
        entry = {"path": m["path"], "line_number": m["line_number"], "content": m["content"]}
        if context > 0:
            entry["context_before"] = m["context_before"]
            entry["context_after"] = m["context_after"]
        result_matches.append(entry)

    result = {"matches": result_matches, "total_count": total}
    if truncated or total > offset + limit:
        result["truncated"] = True
        result["hint"] = f"Use offset={offset + limit} to see more results."
    return result


# ── File search (glob-like) ──────────────────────────────────────────────

def _search_files(pattern: str, root: Path, limit: int, offset: int) -> dict:
    bare_name = pattern.split("/")[-1] if "/" in pattern else pattern

    files: list[str] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(Path(dirpath) / d)]

        for fname in sorted(filenames):
            if fnmatch.fnmatch(fname, bare_name) or fnmatch.fnmatch(fname, f"*{bare_name}*"):
                files.append(str(Path(dirpath) / fname))
                if len(files) >= offset + limit + 1:
                    truncated = True
                    break
        if truncated:
            break

    total = len(files)
    page = files[offset : offset + limit]

    result = {"files": page, "total_count": total}
    if truncated or total > offset + limit:
        result["truncated"] = True
        result["hint"] = f"Use offset={offset + limit} to see more results."
    return result


# ── LangChain tool ───────────────────────────────────────────────────────

class SearchFilesInput(BaseModel):
    pattern: str = Field(
        description="Search pattern: regex for content search, glob for file name search"
    )
    target: str = Field(
        default="content",
        description="Search target: 'content' (grep inside files) or 'files' (find by name)",
    )
    path: str = Field(
        default=".",
        description="Root directory to search in (default: project root)",
    )
    file_glob: str | None = Field(
        default=None,
        description="File name filter for content search, e.g. '*.py' (only used when target='content')",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of results to return (default: 50)",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of results to skip for pagination (default: 0)",
    )
    context: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Lines of context around content matches (default: 0, max: 5)",
    )


class SearchFilesTool(BaseTool):
    """Search for content inside files or find files by name.

    Content search (target='content'): regex grep across text files.
    File search (target='files'): glob pattern match on file names.
    Automatically skips hidden dirs, __pycache__, node_modules, .venv, etc.
    """

    name: str = "search_files"
    args_schema: Type[BaseModel] = SearchFilesInput
    description: str = (
        "Search for a regex pattern inside files (target='content') or "
        "find files by name pattern (target='files'). "
        "Use file_glob to filter by extension when searching content."
    )
    metadata: dict = {"idempotent": True}

    def _run(
        self,
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        context: int = 0,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        resolved = resolve_path(path)

        if not resolved.exists():
            return json.dumps({"error": f"Path not found: {path}"}, ensure_ascii=False)
        if not resolved.is_dir():
            return json.dumps({"error": f"Path is not a directory: {path}"}, ensure_ascii=False)

        if target == "files":
            result = _search_files(pattern, resolved, limit, offset)
        else:
            result = _search_content(pattern, resolved, file_glob, limit, offset, context)

        return json.dumps(result, ensure_ascii=False)


def build_search_files_tool() -> SearchFilesTool:
    tool = SearchFilesTool()
    tool.handle_tool_error = True
    return tool
