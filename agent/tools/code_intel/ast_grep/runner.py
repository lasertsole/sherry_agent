"""ast-grep runner: structural search + rewrite via the sg CLI subprocess.

The two tools are injected into ALL subagents (a core capability, not
RESEARCHER-only). Mirrors oh-my-openagent ast-grep-mcp/src/tools/search.ts and
rewrite.ts.

Rewrite path safety: the child middleware chain does not register ``PathGuard``,
so this module enforces the repository's own path gate itself — every path is
screened with the shared traversal predicate and resolved against the project
root (``resolve_project_path``); traversal or out-of-root paths are refused
before the subprocess runs, so ``dry_run=False`` can only ever write inside the
project. ``SHERRY_SG_ROOT`` overrides that root for sandboxed tests/drills.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from config.features import AST_GREP
from config.path import ROOT_DIR

from .install_hints import sg_binary_not_found_message, sg_install_hints
from .provisioner import provision_sg_binary
from .resolver import resolve_sg_binary

_ROOT_ENV_KEY = "SHERRY_SG_ROOT"
_APPLIED_RE = re.compile(r"Applied\s+(\d+)\s+change", re.IGNORECASE)

_SEARCH_METADATA: dict[str, Any] = {"scope": "subagent_only", "idempotent": True}
_REWRITE_METADATA: dict[str, Any] = {"scope": "subagent_only", "idempotent": False}


class SgSearchInput(BaseModel):
    """Input for ``ast_grep_search``."""

    pattern: str = Field(description="AST pattern (code, not regex); must parse as one AST node.")
    language: str = Field(description="Language of the pattern (e.g. python, typescript).")
    paths: list[str] = Field(description="Root files/directories to search (1-64).")
    globs: list[str] | None = Field(None, description="Optional file glob filters.")
    strictness: str | None = Field(
        None, description="One of cst | smart | ast | relaxed | signature (default smart)."
    )
    max_matches: int | None = Field(None, description="Max results to return (default 50).")


class SgRewriteInput(BaseModel):
    """Input for ``ast_grep_rewrite``."""

    pattern: str = Field(description="AST pattern to match.")
    rewrite: str = Field(description="Replacement pattern (may reuse $VAR from the pattern).")
    language: str = Field(description="Language of the pattern.")
    paths: list[str] = Field(description="Root files/directories (1-64).")
    dry_run: bool = Field(True, description="Preview only (default true); set false to apply.")


def _override_root() -> Path | None:
    """Return the sandboxed root from ``SHERRY_SG_ROOT``, or None when unset."""
    raw = os.environ.get(_ROOT_ENV_KEY, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _resolve_one_path(raw: str) -> tuple[Path | None, str | None]:
    """Resolve one raw path against the project root.

    Returns ``(resolved, None)`` on success or ``(None, error)`` on rejection.
    Without an override this delegates to the canonical
    :func:`resolve_project_path` gate; with ``SHERRY_SG_ROOT`` set it applies the
    same traversal predicate plus containment against that root.
    """
    if not raw or not raw.strip():
        return None, "path must be a non-empty string"

    override = _override_root()
    if override is None:
        from agent.tools.pub_base import PathOutOfBoundsError, resolve_project_path

        try:
            return resolve_project_path(raw), None
        except PathOutOfBoundsError as exc:
            return None, str(exc)

    from pub.func.path import has_traversal_component

    if raw.startswith("~") or has_traversal_component(raw):
        return None, f"path traversal is not allowed: {raw}"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = override / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"path could not be resolved: {exc}"
    if resolved != override and not resolved.is_relative_to(override):
        return None, f"path escapes the project root: {resolved} (root={override})"
    return resolved, None


def _resolve_paths(paths: list[str]) -> tuple[list[str] | None, str | None]:
    """Resolve and validate the whole path list (length-bounded)."""
    if not paths:
        return None, "paths must contain at least one path"
    max_paths = AST_GREP["ast_grep_max_paths"]
    if len(paths) > max_paths:
        return None, f"too many paths: {len(paths)} exceeds the limit of {max_paths}"
    resolved: list[str] = []
    for raw in paths:
        path, error = _resolve_one_path(raw)
        if error is not None:
            return None, error
        assert path is not None
        resolved.append(str(path))
    return resolved, None


def _workdir(resolved_paths: list[str]) -> str:
    """Pick an existing cwd for the subprocess from the first resolved path."""
    first = Path(resolved_paths[0])
    candidate = first.parent if first.is_file() else first
    if candidate.is_dir():
        return str(candidate)
    return str(ROOT_DIR)


def _search_args(
    pattern: str,
    language: str,
    paths: list[str],
    globs: list[str] | None,
    strictness: str | None,
) -> list[str]:
    """Build the ``sg run`` argument vector for a search."""
    args = ["run", "-p", pattern, "--lang", language, "--json=stream"]
    args += ["--strictness", strictness or AST_GREP["ast_grep_strictness_default"]]
    for glob in globs or []:
        args += ["--globs", glob]
    args += paths
    return args


def _rewrite_args(
    pattern: str, rewrite: str, language: str, paths: list[str], dry_run: bool
) -> list[str]:
    """Build the ``sg run`` argument vector for a rewrite.

    ``--json=stream`` previews without writing (it conflicts with ``-U``);
    ``-U`` / ``--update-all`` applies in place and is only ever selected for
    ``dry_run=False``.
    """
    args = ["run", "-p", pattern, "-r", rewrite, "--lang", language]
    args += ["--json=stream"] if dry_run else ["--update-all"]
    args += paths
    return args


def _run_sg(binary: str, args: list[str], timeout_ms: int, cwd: str) -> dict[str, Any]:
    """Run the sg CLI and return ``{ok, records, stdout}`` or ``{ok: False, error}``."""
    from agent.tools.pub_base.env_scrub import scrub_env

    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_ms / 1000,
            env=scrub_env(os.environ.copy()),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ast-grep timed out after {timeout_ms}ms"}
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "error": "ast-grep binary could not be executed"}

    records: list[Any] = []
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue

    if proc.returncode != 0 and not records:
        stderr = (proc.stderr or "").strip()
        return {
            "ok": False,
            "error": stderr or f"ast-grep exited with code {proc.returncode}",
        }
    return {
        "ok": True,
        "records": records,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def _ensure_binary() -> str | None:
    """Resolve the binary or auto-provision it from the pinned release."""
    binary = resolve_sg_binary()
    if binary:
        return binary
    return provision_sg_binary()


def _binary_missing_payload() -> dict[str, Any]:
    """Build the fail-open "binary unavailable" JSON payload."""
    return {
        "error": sg_binary_not_found_message(),
        "install_hints": sg_install_hints(),
    }


def _compact_match(record: Any) -> dict[str, Any]:
    """Project a raw sg JSON record onto a compact, model-friendly shape."""
    if not isinstance(record, dict):
        return {"raw": record}
    rng = record.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    start_line = start.get("line")
    end_line = end.get("line")
    compact: dict[str, Any] = {
        "file": record.get("file"),
        "line": start_line + 1 if isinstance(start_line, int) else None,
        "column": start.get("column"),
        "end_line": end_line + 1 if isinstance(end_line, int) else None,
        "text": record.get("text"),
        "language": record.get("language"),
    }
    if "replacement" in record:
        compact["replacement"] = record["replacement"]
    meta = record.get("metaVariables")
    if meta:
        compact["meta_variables"] = meta
    return compact


class _AstGrepTool(BaseTool):
    """Shared session wiring + JSON helpers for the ast-grep tools."""

    model_config = {"arbitrary_types_allowed": True}

    _session_id: str = PrivateAttr(default="")

    def __init__(self, session_id: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session_id = session_id

    def _run(self, **kwargs: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _arun(self, **kwargs: Any) -> str:
        """Run the blocking subprocess work off the event loop."""
        return await asyncio.to_thread(lambda: self._run(**kwargs))

    def _json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)


class AstGrepSearchTool(_AstGrepTool):
    """Structural code search across files."""

    name: str = "ast_grep_search"
    description: str = (
        "Structural code search with ast-grep: the pattern is code, not regex. "
        "$NAME matches one AST node, $$$NAME matches zero or more nodes. "
        "Use for cross-file structural queries (e.g. every function definition, "
        "every call to a method). Returns JSON matches with file/line/text. "
        'Example: ast_grep_search(pattern="def $FUNC($$$): $$$BODY", '
        'language="python", paths=["src/"])'
    )
    args_schema: type[BaseModel] = SgSearchInput
    metadata: dict[str, Any] = _SEARCH_METADATA

    def _run(
        self,
        pattern: str,
        language: str,
        paths: list[str],
        globs: list[str] | None = None,
        strictness: str | None = None,
        max_matches: int | None = None,
    ) -> str:
        if len(pattern.encode("utf-8")) > AST_GREP["ast_grep_max_pattern_bytes"]:
            return self._json({"error": "pattern too large", "count": 0, "matches": []})

        resolved, error = _resolve_paths(paths)
        if error is not None:
            return self._json({"error": error, "count": 0, "matches": []})

        binary = _ensure_binary()
        if not binary:
            return self._json(_binary_missing_payload())

        result = _run_sg(
            binary,
            _search_args(pattern, language, resolved or [], globs, strictness),
            AST_GREP["ast_grep_timeout_ms"],
            _workdir(resolved or []),
        )
        if not result["ok"]:
            return self._json({"error": result["error"], "count": 0, "matches": []})

        limit = max_matches or AST_GREP["ast_grep_max_matches"]
        records = result["records"]
        matches = [_compact_match(record) for record in records[:limit]]
        return self._json(
            {
                "matches": matches,
                "count": len(matches),
                "truncated": len(records) > limit,
            }
        )


class AstGrepRewriteTool(_AstGrepTool):
    """Structural code rewrite (dry-run preview by default)."""

    name: str = "ast_grep_rewrite"
    description: str = (
        "Structural code rewrite with ast-grep: use $VAR captured by the pattern in "
        "the replacement. dry_run=true (default) PREVIEWS the changes without "
        "writing; set dry_run=false to apply them. Returns the list of changes. "
        'Example: ast_grep_rewrite(pattern="print($MSG)", '
        'rewrite="logger.info($MSG)", language="python", paths=["src/"], dry_run=true)'
    )
    args_schema: type[BaseModel] = SgRewriteInput
    metadata: dict[str, Any] = _REWRITE_METADATA

    def _run(
        self,
        pattern: str,
        rewrite: str,
        language: str,
        paths: list[str],
        dry_run: bool = True,
    ) -> str:
        resolved, error = _resolve_paths(paths)
        if error is not None:
            return self._json({"error": error, "applied": False, "count": 0, "changes": []})

        binary = _ensure_binary()
        if not binary:
            return self._json({**_binary_missing_payload(), "applied": False, "count": 0})

        result = _run_sg(
            binary,
            _rewrite_args(pattern, rewrite, language, resolved or [], dry_run),
            AST_GREP["ast_grep_timeout_ms"],
            _workdir(resolved or []),
        )
        if not result["ok"]:
            return self._json(
                {"error": result["error"], "applied": False, "count": 0, "changes": []}
            )

        if dry_run:
            changes = [_compact_match(record) for record in result["records"]]
            return self._json({"changes": changes, "applied": False, "count": len(changes)})

        # ``sg -U`` reports its change count on stderr ("Applied N changes").
        applied_match = _APPLIED_RE.search(result["stdout"] + result["stderr"])
        applied_count = int(applied_match.group(1)) if applied_match else 0
        return self._json({"changes": [], "applied": True, "count": applied_count})


def build_ast_grep_tools(session_id: str) -> list[BaseTool]:
    """Build the ast-grep tools — injected into ALL subagents (core component)."""
    return [AstGrepSearchTool(session_id=session_id), AstGrepRewriteTool(session_id=session_id)]
