"""Search files tool — ripgrep when available, pure Python otherwise.

Supports two search targets:
  - ``content``: grep-like regex search inside files (returns matches with line numbers)
  - ``files``: glob-like filename search (returns matching file paths)

Two engines, one contract. ripgrep does the scanning when it can be resolved
(``agent/tools/pub_base/rg_resolver.py``) and its engine can express the
pattern; everything else — no ``rg`` on this host, a lookaround or backreference
in the pattern, a hard rg failure — falls through to the pure-Python walk below,
so the tool never depends on an external binary being present. Both engines
return the same envelope and honour the same bounds (time budget, match cap,
prune list), which is what makes paging and the truncation markers trustworthy
either way.
"""

import asyncio
import fnmatch
import json
import re
from pathlib import Path
from typing import override

from langchain_core.callbacks import CallbackManagerForToolRun
from runtime.session.project_dir import current_project_dir
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from agent.tools.file_tools.search_scan import ScanState, SearchQuery, bounded_walk
from agent.tools.pub_base.rg_backend import pattern_needs_python, rg_search, rg_search_files
from agent.tools.pub_base.rg_resolver import resolve_rg
from agent.tools.pub_base import SessionId
from agent.tools.pub_base import (
    tool_error,
    PathOutOfBoundsError,
    resolve_tool_session_id,
    display_path,
    is_text_file,
    enforce_self_path_policy,
    resolve_external_path,
    resolve_workspace_path,
)


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


def _glob_admits(fname: str, include_globs: tuple[str, ...]) -> bool:
    """True when *fname* passes every include glob (empty set admits all)."""
    if not include_globs:
        return True
    return any(fnmatch.fnmatch(fname, glob) for glob in include_globs)


def _search_content(query: SearchQuery, state: ScanState, output_mode: str = "content") -> dict:
    try:
        flags = re.IGNORECASE | (re.DOTALL | re.MULTILINE if query.multiline else 0)
        regex = re.compile(query.pattern, flags)
    except re.error as e:
        return {"error": f"Invalid regex pattern: {e}"}

    matches: list[dict] = []
    # Per-match display paths keyed the same way the rg engine's count mode
    # keys them (display_path of the RESOLVED path), so both engines agree.
    count_paths: list[str] = []

    for dirpath, filenames in bounded_walk(query.root, state):
        for fname in sorted(filenames):
            if state.expired():
                state.note_budget()
                break
            if not _glob_admits(fname, query.include_globs):
                continue

            fpath = dirpath / fname
            if not _stays_within_root(fpath, query.root):
                continue
            if not fpath.is_file() or not is_text_file(fpath):
                continue

            try:
                # newline="" keeps the bytes as-is (no universal-newline
                # translation), so the walk and rg see the SAME text — a
                # multi-line pattern's \n must match a CRLF file the same way
                # it matches an LF one.
                with fpath.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    text = handle.read()
            except (OSError, PermissionError):
                continue

            if query.multiline:
                # The pattern spans lines: scan the file text and report each
                # match at its STARTING line (the same convention rg -U uses).
                for m in regex.finditer(text):
                    start_line = text.count("\n", 0, m.start()) + 1
                    matches.append(
                        {
                            "path": display_path(fpath, query.root),
                            "line_number": start_line,
                            "content": m.group(0)[:500],
                        }
                    )
                    count_paths.append(display_path(fpath))
                    state.note_match(len(matches))
                    if state.stopped:
                        break
            else:
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if regex.search(line):
                        ctx_before = lines[max(0, i - query.context) : i] if query.context else []
                        ctx_after = lines[i + 1 : i + 1 + query.context] if query.context else []
                        matches.append(
                            {
                                "path": display_path(fpath, query.root),
                                "line_number": i + 1,
                                "content": line[:500],
                                "context_before": ctx_before,
                                "context_after": ctx_after,
                            }
                        )
                        count_paths.append(display_path(fpath))
                        state.note_match(len(matches))
                        if state.stopped:
                            break
            if state.stopped:
                break
        if state.stopped:
            break

    if output_mode == "count":
        grouped: dict[str, int] = {}
        for path_key in count_paths:
            grouped[path_key] = grouped.get(path_key, 0) + 1
        return state.finish(
            "counts", [{"path": path, "count": count} for path, count in grouped.items()]
        )

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
            files.append(display_path(matched, query.root))
            state.note_match(len(files))
            if state.stopped:
                break
        if state.stopped:
            break

    return state.finish("files", files)


# ── LangChain tool ───────────────────────────────────────────────────────


#: The ``type`` filter: a language name expands to filename globs BOTH engines
#: apply identically (the rg engine gets them as ``--glob`` includes, so no
#: dependence on rg's own --type table being installed or versioned the same).
_TYPE_GLOBS: dict[str, tuple[str, ...]] = {
    "python": ("*.py", "*.pyi"),
    "typescript": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "javascript": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "rust": ("*.rs",),
    "go": ("*.go",),
    "java": ("*.java",),
    "kotlin": ("*.kt", "*.kts"),
    "swift": ("*.swift",),
    "c": ("*.c", "*.h"),
    "cpp": ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh"),
    "csharp": ("*.cs",),
    "ruby": ("*.rb",),
    "php": ("*.php",),
    "shell": ("*.sh", "*.bash", "*.zsh"),
    "sql": ("*.sql",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass", "*.less"),
    "markdown": ("*.md", "*.markdown"),
    "yaml": ("*.yaml", "*.yml"),
    "json": ("*.json",),
    "toml": ("*.toml",),
}


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
    output_mode: str = Field(
        default="content",
        description=(
            "'content' (default) returns the matching lines; 'count' returns "
            "one match COUNT per file (the page lists files, limit caps files)"
        ),
    )
    multiline: bool = Field(
        default=False,
        description=(
            "Let the pattern match across lines (e.g. 'try.*catch' spanning a "
            "body); matches report their starting line"
        ),
    )
    type: str | None = Field(
        default=None,
        description=(
            "Language filter for content search: python, typescript, javascript, "
            "rust, go, java, kotlin, swift, c, cpp, csharp, ruby, php, shell, "
            "sql, html, css, markdown, yaml, json, toml"
        ),
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
        "Use file_glob to filter by extension, `type` for a language filter "
        "(e.g. python, rust), output_mode='count' for per-file match counts, "
        "and multiline=true when the pattern spans lines."
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
        output_mode: str = "content",
        multiline: bool = False,
        type_filter: str | None = None,
    ) -> str:
        # redundant: path_guard middleware handles this — kept as the second line of defense
        try:
            root = current_project_dir(session_id)
            resolved = resolve_workspace_path(path, root)
        except PathOutOfBoundsError:
            try:
                resolved = resolve_external_path(
                    path, session_id=session_id, action_desc="search directory", mode="read"
                )
            except PathOutOfBoundsError as e:
                return tool_error(str(e))
        else:
            # In-root is not "safe" when the root IS the checkout: the sherry-self
            # read policy still applies (credentials, memory, other sessions).
            # Enforced OUTSIDE the geometry try so a policy refusal cannot fall
            # through into the external path and ask the operator twice.
            try:
                enforce_self_path_policy(resolved, session_id, "search directory", "read")
            except PathOutOfBoundsError as e:
                return tool_error(str(e))

        if not resolved.exists():
            return tool_error(f"Path not found: {display_path(resolved, root)}")
        if not resolved.is_dir():
            return json.dumps(
                {"error": f"Path is not a directory: {display_path(resolved, root)}"},
                ensure_ascii=False,
            )

        if target != "files" and output_mode not in ("content", "count"):
            return tool_error(
                f"Unknown output_mode {output_mode!r}: expected 'content' or 'count'."
            )
        include_globs: tuple[str, ...] = ()
        if target != "files":
            globs: list[str] = []
            if file_glob:
                globs.append(file_glob)
            if type_filter:
                language = type_filter.strip().lower()
                mapped = _TYPE_GLOBS.get(language)
                if mapped is None:
                    return tool_error(
                        f"Unknown type {type_filter!r}; available: "
                        + ", ".join(sorted(_TYPE_GLOBS))
                    )
                globs.extend(mapped)
            include_globs = tuple(dict.fromkeys(globs))

        state = ScanState.start(offset, limit)
        query = (
            SearchQuery(pattern, resolved)
            if target == "files"
            else SearchQuery(pattern, resolved, include_globs, context, multiline)
        )

        result = self._engine(query, target, state, output_mode)
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _engine(
        query: SearchQuery, target: str, state: ScanState, output_mode: str = "content"
    ) -> dict:
        """Run the ripgrep engine, or the Python walk when it cannot serve this query.

        The walk is not a leftover: it is the answer for hosts without ``rg``, for
        patterns using constructs ripgrep's engine lacks, and for the hard failures
        where a partial rg result must not be presented as a complete one.
        """
        if state.expired():
            # Same rule as the walk's first iteration: an exhausted budget scans
            # nothing. The rg engines' watchdog only catches a process that
            # outlives the deadline — a tiny tree can finish before it fires —
            # so a budget already spent must be refused before spawning.
            state.note_budget()
            return state.finish("files" if target == "files" else "matches", [])

        binary = resolve_rg()
        if binary is not None and not (target != "files" and pattern_needs_python(query.pattern)):
            result = (
                rg_search_files(query, state, binary=binary)
                if target == "files"
                else rg_search(query, state, binary=binary, output_mode=output_mode)
            )
            if result is not None:
                return result

        return (
            _search_files(query, state)
            if target == "files"
            else _search_content(query, state, output_mode)
        )

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
        output_mode: str = "content",
        multiline: bool = False,
        type: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = resolve_tool_session_id(session_id, run_manager)
        return self._core(
            pattern,
            target,
            path,
            file_glob,
            limit,
            offset,
            context,
            session_id,
            output_mode,
            multiline,
            type,
        )

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
        output_mode: str = "content",
        multiline: bool = False,
        type: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = resolve_tool_session_id(session_id, run_manager)
        # The scan (walk or child process) is blocking work; run it in a worker
        # thread so a search cannot stall the event loop for its whole budget.
        return await asyncio.to_thread(
            self._core,
            pattern,
            target,
            path,
            file_glob,
            limit,
            offset,
            context,
            session_id,
            output_mode,
            multiline,
            type,
        )


def build_search_files_tool() -> SearchFilesTool:
    tool = SearchFilesTool()
    tool.handle_tool_error = True
    return tool
