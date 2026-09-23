"""LangChain tool wrappers for LSP precise retrieval — RESEARCHER only.

The eight tools (``lsp_goto_definition`` / ``lsp_find_references`` /
``lsp_workspace_symbol`` / ``lsp_call_hierarchy`` / ``lsp_rename`` /
``lsp_diagnostics`` / ``lsp_format`` / ``lsp_status``) are never registered in
``_MAIN_TOOLS_BUILDERS``; ``agent/tools/subagent/spawn/core.py`` injects them into
a child agent only when its functional role is RESEARCHER.

Every tool is **fail-open**: before any request it checks server availability via
:mod:`fallback`, and a missing server / failed start / timed-out request degrades
to a JSON error carrying the install hint and the tree-sitter / regex fallback
suggestion — it never raises. ``line`` / ``character`` inputs are 1-based.

The child middleware chain registers no ``PathGuard``, so the module screens every
path itself (traversal + project-root containment); ``SHERRY_LSP_ROOT`` overrides
the root for sandboxed tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field, PrivateAttr

from config.features import LSP

from .client import LSPClient
from .fallback import LspAvailability, build_fallback_message, check_lsp_availability
from .manager import get_manager
from .protocol import (
    detect_language,
    format_diagnostic,
    format_location,
    format_range,
    format_symbol,
    format_text_edit,
    language_id,
    normalize_workspace_edit,
    path_to_uri,
    to_position,
)

_SCOPE_METADATA: dict[str, Any] = {"scope": "researcher_only", "idempotent": True}
_ROOT_ENV_KEY = "SHERRY_LSP_ROOT"


# ── path resolution ──────────────────────────────────────────────────────────


def _override_root() -> Path | None:
    raw = os.environ.get(_ROOT_ENV_KEY, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _lsp_cwd() -> str:
    override = _override_root()
    return str(override) if override is not None else str(Path.cwd().resolve())


def _resolve_file(raw: str) -> tuple[Path | None, str | None]:
    """Resolve one raw path against the project root, or return an error."""
    if not raw or not raw.strip():
        return None, "file_path must be a non-empty string"

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


# ── shared tool plumbing ─────────────────────────────────────────────────────


class _LspTool(BaseTool):
    """Shared session wiring + availability/acquire/request helpers."""

    model_config = {"arbitrary_types_allowed": True}

    _session_id: str = PrivateAttr(default="")

    def __init__(self, session_id: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session_id = session_id

    def _run(self, **kwargs: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _arun(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(lambda: self._run(**kwargs))

    def _json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _unavailable(self, language: str, availability: LspAvailability, message: str) -> str:
        return self._json(
            {
                "error": build_fallback_message(language, self.name, availability, message),
                "available": False,
            }
        )

    def _resolve_language(self, language: str, file_path: str) -> tuple[str | None, str | None]:
        """Resolve (language, cwd) from an explicit language or a file path."""
        if not language and file_path:
            language = detect_language(file_path) or ""
        if not language:
            return None, "could not determine the language; pass a supported file_path"
        return language, None

    def _open(self, client: LSPClient, file_path: str | None) -> str | None:
        if not file_path:
            return None
        return client.open_file(file_path)

    def _result(self, client: LSPClient, method: str, params: dict) -> tuple[Any, str | None]:
        response = client.request(method, params)
        if "error" in response:
            error = response["error"]
            return None, str(error.get("message", "LSP request failed"))
        return response.get("result"), None


# ── inputs ───────────────────────────────────────────────────────────────────


class GotoDefinitionInput(BaseModel):
    """Input for ``lsp_goto_definition``."""

    file_path: str = Field(description="File containing the symbol.")
    line: int = Field(description="1-based line number of the symbol.")
    character: int = Field(description="1-based column of the symbol.")


class FindReferencesInput(BaseModel):
    """Input for ``lsp_find_references``."""

    file_path: str = Field(description="File containing the symbol.")
    line: int = Field(description="1-based line number of the symbol.")
    character: int = Field(description="1-based column of the symbol.")


class WorkspaceSymbolInput(BaseModel):
    """Input for ``lsp_workspace_symbol``."""

    query: str = Field(description="Fuzzy symbol name to search the workspace for.")
    file_path: str = Field("", description="Optional file to infer the language/root from.")
    language: str = Field("", description="Optional language when no file_path is given.")


class CallHierarchyInput(BaseModel):
    """Input for ``lsp_call_hierarchy``."""

    file_path: str = Field(description="File containing the symbol.")
    line: int = Field(description="1-based line number of the symbol.")
    character: int = Field(description="1-based column of the symbol.")
    direction: str = Field("incoming", description="'incoming' (callers) or 'outgoing' (callees).")


class RenameInput(BaseModel):
    """Input for ``lsp_rename``."""

    file_path: str = Field(description="File containing the symbol.")
    line: int = Field(description="1-based line number of the symbol.")
    character: int = Field(description="1-based column of the symbol.")
    new_name: str = Field(description="New name for the symbol.")
    dry_run: bool = Field(True, description="Preview only (default true); set false to apply.")


class DiagnosticsInput(BaseModel):
    """Input for ``lsp_diagnostics``."""

    file_path: str = Field(description="File to collect diagnostics for.")
    timeout_s: float | None = Field(
        None, description="Seconds to wait for the async publishDiagnostics notification."
    )


class FormatInput(BaseModel):
    """Input for ``lsp_format``."""

    file_path: str = Field(description="File to format.")
    start_line: int | None = Field(None, description="1-based range start line (optional).")
    start_character: int | None = Field(None, description="1-based range start column (optional).")
    end_line: int | None = Field(None, description="1-based range end line (optional).")
    end_character: int | None = Field(None, description="1-based range end column (optional).")
    write: bool = Field(False, description="Preview only (default false); set true to write.")


class StatusInput(BaseModel):
    """Input for ``lsp_status`` — takes no arguments."""


# ── tools ────────────────────────────────────────────────────────────────────


class LspGotoDefinitionTool(_LspTool):
    """Jump to a symbol's definition."""

    name: str = "lsp_goto_definition"
    description: str = (
        "Jump to the definition of a symbol using the language server. Precise, "
        "type-aware result — prefer it over grep once you know the file/line/column. "
        "line/character are 1-based. Returns the target location(s). "
        'Example: lsp_goto_definition("agent/core.py", 42, 5)'
    )
    args_schema: type[BaseModel] = GotoDefinitionInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, file_path: str, line: int, character: int) -> str:
        resolved, error = _resolve_file(file_path)
        if error is not None:
            return self._json({"error": error, "available": True})
        assert resolved is not None
        language, error = self._resolve_language("", str(resolved))
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        open_error = self._open(client, str(resolved))
        if open_error is not None:
            return self._json({"error": open_error, "available": True})
        params = client.position_params(str(resolved), line, character)
        result, error = self._result(client, "textDocument/definition", params)
        if error is not None:
            return self._json({"error": error, "available": True})
        locations = _as_locations(result)
        return self._json({"definitions": locations, "count": len(locations)})


class LspFindReferencesTool(_LspTool):
    """Find all references to a symbol."""

    name: str = "lsp_find_references"
    description: str = (
        "Find all references to a symbol using the language server. Precise, "
        "type-aware result. line/character are 1-based. Returns reference locations. "
        'Example: lsp_find_references("agent/core.py", 42, 5)'
    )
    args_schema: type[BaseModel] = FindReferencesInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, file_path: str, line: int, character: int) -> str:
        resolved, error = _resolve_file(file_path)
        if error is not None:
            return self._json({"error": error, "available": True})
        assert resolved is not None
        language, error = self._resolve_language("", str(resolved))
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        open_error = self._open(client, str(resolved))
        if open_error is not None:
            return self._json({"error": open_error, "available": True})
        params = client.position_params(str(resolved), line, character)
        params["context"] = {"includeDeclaration": True}
        result, error = self._result(client, "textDocument/references", params)
        if error is not None:
            return self._json({"error": error, "available": True})
        locations = _as_locations(result)
        return self._json({"references": locations, "count": len(locations)})


class LspWorkspaceSymbolTool(_LspTool):
    """Search workspace symbols by name."""

    name: str = "lsp_workspace_symbol"
    description: str = (
        "Search the workspace for symbols by fuzzy name using the language server. "
        "Returns symbol names with their locations. Provide file_path (preferred) or "
        "language to pick the server. "
        'Example: lsp_workspace_symbol("build_agent", file_path="agent/core.py")'
    )
    args_schema: type[BaseModel] = WorkspaceSymbolInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, query: str, file_path: str = "", language: str = "") -> str:
        resolved: Path | None = None
        if file_path:
            resolved, error = _resolve_file(file_path)
            if error is not None:
                return self._json({"error": error, "available": True})
        language, error = self._resolve_language(language, str(resolved) if resolved else "")
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        result, error = self._result(client, "workspace/symbol", {"query": query})
        if error is not None:
            return self._json({"error": error, "available": True})
        symbols = [format_symbol(item) for item in result] if isinstance(result, list) else []
        limit = LSP["lsp_max_results"]
        symbols = [symbol for symbol in symbols if symbol.get("name")]
        return self._json(
            {
                "symbols": symbols[:limit],
                "count": min(len(symbols), limit),
                "truncated": len(symbols) > limit,
            }
        )


class LspCallHierarchyTool(_LspTool):
    """Show incoming (callers) or outgoing (callees) calls of a symbol."""

    name: str = "lsp_call_hierarchy"
    description: str = (
        "Show the call hierarchy of a symbol: direction='incoming' lists callers, "
        "direction='outgoing' lists callees. line/character are 1-based. "
        'Example: lsp_call_hierarchy("agent/core.py", 42, 5, direction="incoming")'
    )
    args_schema: type[BaseModel] = CallHierarchyInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, file_path: str, line: int, character: int, direction: str = "incoming") -> str:
        resolved, error = _resolve_file(file_path)
        if error is not None:
            return self._json({"error": error, "available": True})
        assert resolved is not None
        language, error = self._resolve_language("", str(resolved))
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        normalized = direction.strip().lower()
        if normalized not in ("incoming", "outgoing"):
            return self._json(
                {"error": "direction must be 'incoming' or 'outgoing'", "available": True}
            )

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        open_error = self._open(client, str(resolved))
        if open_error is not None:
            return self._json({"error": open_error, "available": True})
        params = client.position_params(str(resolved), line, character)
        items, error = self._result(client, "textDocument/prepareCallHierarchy", params)
        if error is not None:
            return self._json({"error": error, "available": True})
        if not items:
            return self._json({"direction": normalized, "calls": [], "count": 0})

        method = f"callHierarchy/{normalized}Calls"
        calls, error = self._result(client, method, {"item": items[0]})
        if error is not None:
            return self._json({"error": error, "available": True})
        formatted = _format_calls(calls, normalized)
        return self._json({"direction": normalized, "calls": formatted, "count": len(formatted)})


class LspRenameTool(_LspTool):
    """Rename a symbol across the workspace (preview by default)."""

    name: str = "lsp_rename"
    description: str = (
        "Rename a symbol across the workspace via the language server. dry_run=true "
        "(default) PREVIEWS the resulting WorkspaceEdit and writes nothing; set "
        "dry_run=false to apply the edits inside the project. line/character are 1-based. "
        'Example: lsp_rename("agent/core.py", 42, 5, "run_agent", dry_run=true)'
    )
    args_schema: type[BaseModel] = RenameInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(
        self,
        file_path: str,
        line: int,
        character: int,
        new_name: str,
        dry_run: bool = True,
    ) -> str:
        if not new_name or not new_name.strip():
            return self._json({"error": "new_name must be a non-empty string", "available": True})
        resolved, error = _resolve_file(file_path)
        if error is not None:
            return self._json({"error": error, "available": True})
        assert resolved is not None
        language, error = self._resolve_language("", str(resolved))
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        open_error = self._open(client, str(resolved))
        if open_error is not None:
            return self._json({"error": open_error, "available": True})
        params = client.position_params(str(resolved), line, character)
        params["newName"] = new_name
        result, error = self._result(client, "textDocument/rename", params)
        if error is not None:
            return self._json({"error": error, "available": True})

        renames = normalize_workspace_edit(result)
        edit_count = sum(len(group["edits"]) for group in renames)
        payload: dict[str, Any] = {
            "renames": renames,
            "count": len(renames),
            "edit_count": edit_count,
            "applied": False,
        }
        if dry_run:
            payload["note"] = "Preview only; pass dry_run=false to apply these edits."
            return self._json(payload)

        payload.update(_apply_workspace_edit(renames))
        payload["applied"] = True
        return self._json(payload)


class LspDiagnosticsTool(_LspTool):
    """Collect a file's published diagnostics (errors / warnings)."""

    name: str = "lsp_diagnostics"
    description: str = (
        "Get the language server's diagnostics (errors/warnings) for a file. "
        "Diagnostics are delivered asynchronously, so the tool opens the file and "
        "waits for the server's publishDiagnostics notification. Returns severity, "
        "message, and 1-based range per diagnostic. "
        'Example: lsp_diagnostics("agent/core.py")'
    )
    args_schema: type[BaseModel] = DiagnosticsInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, file_path: str, timeout_s: float | None = None) -> str:
        resolved, error = _resolve_file(file_path)
        if error is not None:
            return self._json({"error": error, "available": True})
        assert resolved is not None
        language, error = self._resolve_language("", str(resolved))
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        open_error = self._open(client, str(resolved))
        if open_error is not None:
            return self._json({"error": open_error, "available": True})
        uri = path_to_uri(str(resolved))
        window = timeout_s if timeout_s is not None else LSP["lsp_diagnostics_timeout_s"]
        client.wait_diagnostics(uri, window)
        cached = client.cached_diagnostics(uri)
        raw = cached or []
        diagnostics = [item for d in raw if (item := format_diagnostic(d)) is not None]
        summary = {"error": 0, "warning": 0, "information": 0, "hint": 0, "unknown": 0}
        for item in diagnostics:
            summary[item["severity"]] = summary.get(item["severity"], 0) + 1
        return self._json(
            {
                "diagnostics": diagnostics,
                "count": len(diagnostics),
                "summary": summary,
                "timed_out": cached is None,
            }
        )


class LspFormatTool(_LspTool):
    """Format a file (or range) via the language server; preview by default."""

    name: str = "lsp_format"
    description: str = (
        "Format a file via the language server (textDocument/formatting, or "
        "rangeFormatting when a 1-based range is given). write=false (default) "
        "PREVIEWS the edits; set write=true to apply them. If the server does not "
        "support formatting the result reports supported=false — it never claims a "
        "file was formatted when it was not. "
        'Example: lsp_format("agent/core.py", write=false)'
    )
    args_schema: type[BaseModel] = FormatInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(
        self,
        file_path: str,
        start_line: int | None = None,
        start_character: int | None = None,
        end_line: int | None = None,
        end_character: int | None = None,
        write: bool = False,
    ) -> str:
        resolved, error = _resolve_file(file_path)
        if error is not None:
            return self._json({"error": error, "available": True})
        assert resolved is not None
        language, error = self._resolve_language("", str(resolved))
        if error is not None:
            return self._json({"error": error, "available": True})
        assert language is not None

        availability, message = check_lsp_availability(language, _lsp_cwd())
        if availability != "available":
            return self._unavailable(language, availability, message)
        client, error = get_manager().acquire(language, _lsp_cwd(), message)
        if client is None:
            return self._unavailable(language, "not_installed", error)

        open_error = self._open(client, str(resolved))
        if open_error is not None:
            return self._json({"error": open_error, "available": True})

        range_args = (start_line, start_character, end_line, end_character)
        options = {"tabSize": 4, "insertSpaces": True}
        if all(value is not None for value in range_args):
            method = "textDocument/rangeFormatting"
            params: dict[str, Any] = {
                "textDocument": {"uri": path_to_uri(str(resolved))},
                "options": options,
                "range": {
                    "start": to_position(start_line, start_character),
                    "end": to_position(end_line, end_character),
                },
            }
        else:
            method = "textDocument/formatting"
            params = {"textDocument": {"uri": path_to_uri(str(resolved))}, "options": options}

        result, error = self._result(client, method, params)
        if error is not None:
            return self._json(
                {"supported": False, "applied": False, "available": True, "message": error}
            )
        if result is None:
            return self._json(
                {
                    "supported": False,
                    "applied": False,
                    "available": True,
                    "message": f"{language} language server does not support formatting",
                }
            )

        raw_edits = result if isinstance(result, list) else []
        edits = [item for e in raw_edits if (item := format_text_edit(e)) is not None]
        payload: dict[str, Any] = {
            "supported": True,
            "method": method,
            "edits": edits,
            "count": len(edits),
            "applied": False,
        }
        if not write:
            payload["note"] = "Preview only; pass write=true to apply these edits."
            return self._json(payload)

        count, error = _apply_text_edits(resolved, edits)
        if error is not None:
            payload["error"] = error
            return self._json(payload)
        payload.update({"applied": True, "applied_edits": count, "file": str(resolved)})
        return self._json(payload)


class LspStatusTool(_LspTool):
    """Report every configured / installed / active language server."""

    name: str = "lsp_status"
    description: str = (
        "List every configured LSP language server with its honest status: "
        "'available' (binary found), 'not_installed' (configured but the binary is "
        "missing), or 'not_configured', plus the resolved binary or install hint and "
        "whether a server is currently running. No language server is started by this "
        "tool. Example: lsp_status()"
    )
    args_schema: type[BaseModel] = StatusInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self) -> str:
        cwd = _lsp_cwd()
        active = get_manager().stats()
        active_languages = {entry["language"] for entry in active if entry.get("alive")}
        servers: list[dict] = []
        summary = {"available": 0, "not_installed": 0, "not_configured": 0}
        for language, spec in LSP["lsp_supported_servers"].items():
            availability, message = check_lsp_availability(language, cwd)
            summary[availability] = summary.get(availability, 0) + 1
            entry: dict[str, Any] = {
                "language": language,
                "server": spec["command"][0],
                "language_id": language_id(language),
                "status": availability,
                "active": language in active_languages,
            }
            if availability == "available":
                entry["binary"] = message
            else:
                entry["install_hint"] = message
            servers.append(entry)
        return self._json(
            {
                "servers": servers,
                "count": len(servers),
                "summary": summary,
                "active_servers": active,
            }
        )


# ── result formatting ────────────────────────────────────────────────────────


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _apply_text_edits(path: Path, edits: list[dict]) -> tuple[int, str | None]:
    """Apply 1-based-range ``TextEdit``s to *path*; returns ``(count, error)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return 0, f"cannot read {path}: {exc}"

    offsets = _line_offsets(text)
    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        rng = edit.get("range") or {}
        start = _offset(offsets, len(text), rng.get("start_line"), rng.get("start_character"))
        end = _offset(offsets, len(text), rng.get("end_line"), rng.get("end_character"))
        spans.append((start, end, edit.get("new_text", "")))

    for start, end, replacement in sorted(spans, key=lambda span: span[0], reverse=True):
        text = f"{text[:start]}{replacement}{text[end:]}"

    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return 0, f"cannot write {path}: {exc}"
    return len(spans), None


def _offset(offsets: list[int], text_len: int, line: int | None, character: int | None) -> int:
    if line is None:
        return text_len
    base = offsets[line - 1] if 0 < line <= len(offsets) else text_len
    return min(base + max((character or 1) - 1, 0), text_len)


def _apply_workspace_edit(groups: list[dict]) -> dict:
    """Apply grouped WorkspaceEdit entries, honoring the project-root path gate."""
    applied = 0
    files_written: list[str] = []
    skipped: list[dict] = []
    for group in groups:
        resolved, error = _resolve_file(group.get("path", ""))
        if error is not None or resolved is None:
            skipped.append({"path": group.get("path"), "reason": error or "unresolved"})
            continue
        count, error = _apply_text_edits(resolved, group.get("edits", []))
        if error is not None:
            skipped.append({"path": group.get("path"), "reason": error})
            continue
        applied += count
        files_written.append(str(resolved))
    return {"applied_edits": applied, "files_written": files_written, "skipped": skipped}


def _as_locations(result: Any) -> list[dict]:
    if not isinstance(result, list):
        result = [result] if result else []
    locations = [format_location(item) for item in result if isinstance(item, dict)]
    limit = LSP["lsp_max_results"]
    return [location for location in locations if location][:limit]


def _format_calls(calls: Any, direction: str) -> list[dict]:
    if not isinstance(calls, list):
        return []
    counterpart_key = "from" if direction == "incoming" else "to"
    formatted: list[dict] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        item = call.get(counterpart_key)
        if not isinstance(item, dict):
            continue
        location = format_location(item)
        entry: dict[str, Any] = {"name": item.get("name"), "kind": item.get("kind")}
        if location is not None:
            entry.update(location)
        if direction == "incoming" and isinstance(call.get("fromRanges"), list):
            entry["call_sites"] = [
                site
                for site in (format_range(raw) for raw in call["fromRanges"])
                if site is not None
            ]
        formatted.append(entry)
    limit = LSP["lsp_max_results"]
    return formatted[:limit]


def build_lsp_tools(session_id: str) -> list[BaseTool]:
    """Build the eight RESEARCHER-only LSP tools (never raise; fail-open)."""
    try:
        return [
            LspGotoDefinitionTool(session_id=session_id),
            LspFindReferencesTool(session_id=session_id),
            LspWorkspaceSymbolTool(session_id=session_id),
            LspCallHierarchyTool(session_id=session_id),
            LspRenameTool(session_id=session_id),
            LspDiagnosticsTool(session_id=session_id),
            LspFormatTool(session_id=session_id),
            LspStatusTool(session_id=session_id),
        ]
    except Exception:  # noqa: BLE001 - a broken tool build must not abort a spawn
        logger.warning("lsp: tool build failed; continuing without LSP tools", exc_info=True)
        return []
