"""LangChain tool wrappers for LSP precise retrieval — RESEARCHER only.

The four tools (``lsp_goto_definition`` / ``lsp_find_references`` /
``lsp_workspace_symbol`` / ``lsp_call_hierarchy``) are never registered in
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
from .protocol import detect_language, format_location, format_range, format_symbol

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


# ── result formatting ────────────────────────────────────────────────────────


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
    """Build the four RESEARCHER-only LSP tools (never raise; fail-open)."""
    try:
        return [
            LspGotoDefinitionTool(session_id=session_id),
            LspFindReferencesTool(session_id=session_id),
            LspWorkspaceSymbolTool(session_id=session_id),
            LspCallHierarchyTool(session_id=session_id),
        ]
    except Exception:  # noqa: BLE001 - a broken tool build must not abort a spawn
        logger.warning("lsp: tool build failed; continuing without LSP tools", exc_info=True)
        return []
