"""Search files tool — pure Python, cross-platform.

Supports two search targets:
  - ``content``: grep-like regex search inside files (returns matches with line numbers)
  - ``files``: glob-like filename search (returns matching file paths)

No external dependencies (rg, grep, find) required.
"""

import fnmatch
import json
import re
from pathlib import Path
from typing import Annotated, override

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import InjectedState
from pydantic import BaseModel, Field

from agent.tools.file_tools.search_scan import ScanState, SearchQuery, bounded_walk
from agent.tools.pub_base import (
    PathOutOfBoundsError,
    _extract_session_id,
    display_path,
    is_text_file,
    resolve_external_path,
    resolve_project_path,
)

SessionId = Annotated[str, InjectedState("session_id")]


# ── Content search (grep-like) ───────────────────────────────────────────


def _stays_within_root(candidate: Path, root: Path) -> bool:
    """Containment check: reject results whose real path escapes the search root.

    ``os.walk`` does not descend directory symlinks, but file symlinks still
    surface in listings and reads; a link pointing outside the searched tree
    must never produce a result. The root is always an already-resolved path
    (project searches are additionally bounded by ROOT_DIR), so this also
    keeps allowlisted external-directory searches working.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _search_content(query: SearchQuery, state: ScanState) -> dict:
    try:
        regex = re.compile(query.pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Invalid regex pattern: {e}"}

    matches: list[dict] = []

    for dirpath, filenames in bounded_walk(query.root, state):
        for fname in sorted(filenames):
            if state.expired():
                state.note_budget()
                break
            if query.file_glob and not fnmatch.fnmatch(fname, query.file_glob):
                continue

            fpath = dirpath / fname
            if not _stays_within_root(fpath, query.root):
                continue
            if not fpath.is_file() or not is_text_file(fpath):
                continue

            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, PermissionError):
                continue

            for i, line in enumerate(lines):
                if regex.search(line):
                    ctx_before = lines[max(0, i - query.context) : i] if query.context else []
                    ctx_after = lines[i + 1 : i + 1 + query.context] if query.context else []
                    matches.append(
                        {
                            "path": display_path(fpath),
                            "line_number": i + 1,
                            "content": line[:500],
                            "context_before": ctx_before,
                            "context_after": ctx_after,
                        }
                    )
                    state.note_match(len(matches))
                    if state.stopped:
                        break
            if state.stopped:
                break
        if state.stopped:
            break

    result = state.finish("matches", matches)
    if query.context <= 0:
        for entry in result["matches"]:
            entry.pop("context_before", None)
            entry.pop("context_after", None)
    return result


# ── File search (glob-like) ──────────────────────────────────────────────


def _search_files(query: SearchQuery, state: ScanState) -> dict:
    bare_name = query.pattern.split("/")[-1] if "/" in query.pattern else query.pattern

    files: list[str] = []

    for dirpath, filenames in bounded_walk(query.root, state):
        for fname in sorted(filenames):
            if state.expired():
                state.note_budget()
                break
            if not (fnmatch.fnmatch(fname, bare_name) or fnmatch.fnmatch(fname, f"*{bare_name}*")):
                continue
            matched = dirpath / fname
            if not _stays_within_root(matched, query.root):
                continue
            files.append(display_path(matched))
            state.note_match(len(files))
            if state.stopped:
                break
        if state.stopped:
            break

    return state.finish("files", files)


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
    session_id: SessionId = ""


class SearchFilesTool(BaseTool):
    """Search for content inside files or find files by name.

    Content search (target='content'): regex grep across text files.
    File search (target='files'): glob pattern match on file names.
    Automatically skips hidden dirs, __pycache__, node_modules, .venv, etc.
    """

    name: str = "search_files"
    args_schema: type[BaseModel] = SearchFilesInput
    description: str = (
        "Search for a regex pattern inside files (target='content') or "
        "find files by name pattern (target='files'). "
        "Use file_glob to filter by extension when searching content."
    )
    metadata: dict = {"idempotent": True}

    # ── shared core ────────────────────────────────────────────────────────

    def _core(
        self,
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        context: int = 0,
        session_id: str = "",
    ) -> str:
        # redundant: path_guard middleware handles this — kept as the second line of defense
        try:
            resolved = resolve_project_path(path)
        except PathOutOfBoundsError:
            try:
                resolved = resolve_external_path(
                    path, session_id=session_id, action_desc="search directory"
                )
            except PathOutOfBoundsError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        if not resolved.exists():
            return json.dumps(
                {"error": f"Path not found: {display_path(resolved)}"}, ensure_ascii=False
            )
        if not resolved.is_dir():
            return json.dumps(
                {"error": f"Path is not a directory: {display_path(resolved)}"}, ensure_ascii=False
            )

        state = ScanState.start(offset, limit)
        if target == "files":
            result = _search_files(SearchQuery(pattern, resolved), state)
        else:
            result = _search_content(SearchQuery(pattern, resolved, file_glob, context), state)

        return json.dumps(result, ensure_ascii=False)

    @override
    def _run(
        self,
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        context: int = 0,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(pattern, target, path, file_glob, limit, offset, context, session_id)

    @override
    async def _arun(
        self,
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        context: int = 0,
        session_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(pattern, target, path, file_glob, limit, offset, context, session_id)


def build_search_files_tool() -> SearchFilesTool:
    tool = SearchFilesTool()
    tool.handle_tool_error = True
    return tool
