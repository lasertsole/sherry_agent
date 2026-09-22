"""LangChain tool wrappers for the code intelligence index — RESEARCHER only.

The four tools (``explore`` / ``callers`` / ``callees`` / ``impact``) are never
registered in ``_MAIN_TOOLS_BUILDERS``; ``agent/tools/subagent/spawn/core.py``
injects them into a child agent only when its functional role is RESEARCHER.
Each carries ``metadata={"scope": "researcher_only"}`` so the tag is visible to
policy layers, and the ``sh``-style outputs are JSON strings, matching the rest
of the tool surface.
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

from config.features.agent_side import CODE_INTEL, CodeIntelConfig
from config.path import CODE_INTEL_DIR

from .query import CalleeInfo, CallerInfo, CodeQuery, ExploreResult, ImpactResult

_ROOT_ENV_KEY = "SHERRY_CODE_INTEL_ROOT"
_DB_ENV_KEY = "SHERRY_CODE_INTEL_DB"
_SCOPE_METADATA: dict[str, Any] = {"scope": "researcher_only", "idempotent": True}


def _resolve_root() -> Path:
    """Resolve the index root: explicit env override, else the process cwd."""
    override = os.environ.get(_ROOT_ENV_KEY, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def _resolve_db_path(config: CodeIntelConfig) -> str:
    override = os.environ.get(_DB_ENV_KEY, "").strip()
    if override:
        return str(Path(override).expanduser())
    return config["code_intel_index_db_path"] or str(CODE_INTEL_DIR / "index.db")


class ExploreInput(BaseModel):
    """Input for the ``explore`` tool."""

    query: str = Field(description="Concept, symbol name, or natural-language intent to explore.")


class CallersInput(BaseModel):
    """Input for the ``callers`` tool."""

    symbol: str = Field(description="Symbol name whose callers should be listed.")


class CalleesInput(BaseModel):
    """Input for the ``callees`` tool."""

    symbol: str = Field(description="Symbol name whose outgoing calls should be listed.")


class ImpactInput(BaseModel):
    """Input for the ``impact`` tool."""

    symbol: str = Field(description="Symbol name whose blast radius should be analysed.")


class _CodeIntelTool(BaseTool):
    """Shared engine/root/config wiring for the four tools."""

    model_config = {"arbitrary_types_allowed": True}

    _engine: CodeQuery = PrivateAttr()
    _config: CodeIntelConfig = PrivateAttr()
    _root: Path = PrivateAttr()
    _session_id: str = PrivateAttr(default="")

    def __init__(
        self,
        engine: CodeQuery,
        config: CodeIntelConfig,
        root: Path,
        session_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._engine = engine
        self._config = config
        self._root = root
        self._session_id = session_id

    def _run(self, **kwargs: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _arun(self, **kwargs: Any) -> str:
        """Run the blocking index/query work off the event loop."""
        return await asyncio.to_thread(lambda: self._run(**kwargs))

    def _json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)


class ExploreTool(_CodeIntelTool):
    """Fuzzy intent → symbol source + immediate call paths."""

    name: str = "explore"
    description: str = (
        "Find code related to a concept, function name, or natural language query. "
        "Returns source code plus immediate callers/callees for the most relevant symbols. "
        "Use FIRST when you need to understand how something works, where it is defined, or "
        "what calls it. Falls back to search_files when no symbol matches. "
        'Example: explore("database connection pooling")'
    )
    args_schema: type[BaseModel] = ExploreInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, query: str) -> str:
        result = self._engine.explore(query, self._root)
        return self._json(_explore_payload(result))


class CallersTool(_CodeIntelTool):
    """List functions/methods that call a symbol."""

    name: str = "callers"
    description: str = (
        "Find all functions/methods that call the given symbol. "
        "Returns caller name, file, line, and the call site line. "
        'Example: callers("built_agent")'
    )
    args_schema: type[BaseModel] = CallersInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, symbol: str) -> str:
        rows = self._engine.callers(symbol, self._root)
        return self._json(_callers_payload(symbol, rows))


class CalleesTool(_CodeIntelTool):
    """List functions/methods called by a symbol."""

    name: str = "callees"
    description: str = (
        "Find all functions/methods called by the given symbol. "
        "Returns callee name, resolved target file/line when known, and the call site line. "
        'Example: callees("built_agent")'
    )
    args_schema: type[BaseModel] = CalleesInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, symbol: str) -> str:
        rows = self._engine.callees(symbol, self._root)
        return self._json(_callees_payload(symbol, rows))


class ImpactTool(_CodeIntelTool):
    """Blast radius of modifying a symbol."""

    name: str = "impact"
    description: str = (
        "Analyse the blast radius of modifying the given symbol by traversing the call graph "
        "upward (callers direction) to the configured depth. Use before refactoring to "
        'understand what might break. Example: impact("build_main_tools")'
    )
    args_schema: type[BaseModel] = ImpactInput
    metadata: dict[str, Any] = _SCOPE_METADATA

    def _run(self, symbol: str) -> str:
        result = self._engine.impact(symbol, self._root)
        return self._json(_impact_payload(result))


def _explore_payload(result: ExploreResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": result.query,
        "count": len(result.entries),
        "truncated": result.truncated,
        "results": [
            {
                "name": entry.symbol.name,
                "kind": entry.symbol.kind,
                "file": entry.symbol.file_path,
                "line_start": entry.symbol.line_start,
                "line_end": entry.symbol.line_end,
                "score": entry.symbol.score,
                "doc": entry.symbol.doc_string,
                "source": entry.source,
                "callers": entry.callers,
                "callees": entry.callees,
            }
            for entry in result.entries
        ],
    }
    if result.suggestion:
        payload["suggestion"] = result.suggestion
    return payload


def _callers_payload(symbol: str, rows: list[CallerInfo]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "count": len(rows),
        "callers": [
            {
                "name": row.name,
                "kind": row.kind,
                "file": row.file_path,
                "line": row.line_start,
                "call_line": row.call_line,
            }
            for row in rows
        ],
    }


def _callees_payload(symbol: str, rows: list[CalleeInfo]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "count": len(rows),
        "callees": [
            {
                "name": row.name,
                "target_file": row.target_file,
                "target_line": row.target_line,
                "call_line": row.call_line,
                "resolved": row.resolved,
            }
            for row in rows
        ],
    }


def _impact_payload(result: ImpactResult) -> dict[str, Any]:
    return {
        "symbol": result.root_symbol,
        "max_depth": result.max_depth,
        "count": len(result.entries),
        "truncated": result.truncated,
        "affected": [
            {
                "name": entry.symbol.name,
                "kind": entry.symbol.kind,
                "file": entry.symbol.file_path,
                "line": entry.symbol.line_start,
                "depth": entry.depth,
                "via": entry.via,
            }
            for entry in result.entries
        ],
    }


def build_code_intel_tools(
    session_id: str,
    *,
    config: CodeIntelConfig | None = None,
    root: Path | None = None,
    db_path: str | None = None,
) -> list[BaseTool]:
    """Build the four RESEARCHER-only code intelligence tools.

    Optional overrides exist for tests; production callers pass only
    ``session_id`` and resolve the root from the process environment.
    """
    cfg = config or CODE_INTEL
    try:
        engine = CodeQuery(db_path or _resolve_db_path(cfg), cfg)
    except Exception as exc:  # noqa: BLE001 - missing tooling must not break a spawn
        logger.warning("code_intel: tools unavailable: {}", exc)
        return []
    resolved_root = Path(root).resolve() if root is not None else _resolve_root()
    return [
        ExploreTool(engine, cfg, resolved_root, session_id),
        CallersTool(engine, cfg, resolved_root, session_id),
        CalleesTool(engine, cfg, resolved_root, session_id),
        ImpactTool(engine, cfg, resolved_root, session_id),
    ]
