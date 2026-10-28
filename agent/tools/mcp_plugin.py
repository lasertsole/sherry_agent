"""MCP server discovery + tool building.

Two sources layer into one namespace (user overrides bundled on a name
clash):

* **Bundled defaults** — the read-only ``plugins/mcp_server/config.json``
  (shipped with the install; currently a placeholder ``{}``). Its ``$here/``
  resolves to its own directory, exactly as before.
* **The user drop folder** — ``APP_HOME/mcp_servers/``, the ONE writable place
  a user adds MCP servers in a unified (read-only) install. Two shapes:
  a flat ``<name>.json`` (the file IS the server spec) and a self-contained
  ``<name>/config.json`` directory whose ``$here/`` points at the directory
  itself, so ``server.py`` can travel beside its config.

Every entry is parsed in ISOLATION: a bad JSON file (users hand-write these)
skips exactly one server and never takes the whole load down. The scan is
pure discovery — no process is spawned; connections happen in
:func:`build_mcp_tools`, also per server, so a crashing server costs only
its own tools.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pub.func import run_async
from langchain_core.tools import BaseTool
from loguru import logger

_HERE_DIR = Path(__file__).resolve().parent  # tools/
_PROJECT_ROOT = _HERE_DIR.parent  # sherry_agent/
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import MCP_SERVERS_DIR, PLUGINS_PATH  # noqa: E402  (delayed import: after sys.path setup above, which guarantees `config` is importable when this file runs outside the package context)

__all__ = ["McpServerEntry", "build_mcp_tools", "scan_mcp_servers"]

_BUNDLED_CONFIG = PLUGINS_PATH / "mcp_server" / "config.json"


@dataclass(frozen=True)
class McpServerEntry:
    """One discovered MCP server (the API's and the loader's shared shape).

    ``spec`` is None exactly when ``error`` is set — a failed entry is
    reported (the UI marks it ⚠), never silently dropped.
    """

    name: str
    source: str  # "bundled" | "user"
    spec: dict | None
    here: Path
    error: str | None = None


def _read_json_file(path: Path) -> dict:
    """Parse one JSON file; a non-object body is an error like bad syntax."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    return raw


def _bundled_entries() -> list[McpServerEntry]:
    """The shipped defaults from the read-only install tree.

    The legacy loader auto-created an empty config.json here; a read-only
    install tree cannot (and the repo tracks the file anyway), so a missing
    file simply means no bundled servers.
    """
    if not _BUNDLED_CONFIG.is_file():
        return []
    try:
        raw = _read_json_file(_BUNDLED_CONFIG)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            McpServerEntry(
                name="(bundled)",
                source="bundled",
                spec=None,
                here=_BUNDLED_CONFIG.parent,
                error=str(exc),
            )
        ]
    return [
        McpServerEntry(name=str(name), source="bundled", spec=spec, here=_BUNDLED_CONFIG.parent)
        for name, spec in raw.items()
        if isinstance(spec, dict)
    ]


def _parse_user_entry(entry: Path) -> McpServerEntry | None:
    """One drop-folder entry, in either shape; ``None`` skips silently.

    Non-JSON files and directories without a ``config.json`` are not user
    mistakes worth reporting (a README lives there by design) — they just
    never appear in the list.
    """
    if entry.is_file() and entry.suffix == ".json":
        name, here, config_path = entry.stem, entry.parent, entry
    elif entry.is_dir() and (entry / "config.json").is_file():
        name, here, config_path = entry.name, entry, entry / "config.json"
    else:
        return None
    try:
        spec = _read_json_file(config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return McpServerEntry(name=name, source="user", spec=None, here=here, error=str(exc))
    return McpServerEntry(name=name, source="user", spec=spec, here=here)


def scan_mcp_servers() -> list[McpServerEntry]:
    """Every discovered MCP server: bundled defaults, then the user folder.

    Sorted per source for a deterministic scan order (a name clash means the
    user entry wins at merge time, regardless of order). This is discovery
    only — nothing is spawned or connected.
    """
    entries = _bundled_entries()
    if MCP_SERVERS_DIR.is_dir():
        for path in sorted(MCP_SERVERS_DIR.iterdir()):
            parsed = _parse_user_entry(path)
            if parsed is not None:
                entries.append(parsed)
    return entries


def _resolve_placeholders(spec: dict, here: Path) -> dict:
    """``$sys.executable`` → the running interpreter; ``$here/`` → *here*.

    Per-entry on purpose: a user drop folder entry resolves ``$here/`` to its
    OWN directory (flat file → its parent, subdirectory → the directory), so
    ``server.py`` can travel beside its ``config.json``. The bundled file's
    ``$here/`` keeps resolving to ``plugins/mcp_server/`` — its own directory
    — which is byte-for-byte the legacy behavior.
    """
    resolved = dict(spec)
    here_str = f"{here.as_posix().rstrip('/')}/"
    if resolved.get("command") == "$sys.executable":
        resolved["command"] = sys.executable
    args = resolved.get("args")
    if args:
        resolved["args"] = [
            a.replace("$here/", here_str) if isinstance(a, str) else a for a in args
        ]
    return resolved


def _load_config() -> dict:
    """The merged, placeholder-resolved server map for ``MultiServerMCPClient``.

    A failed entry logs and skips (error isolation): one hand-written bad
    JSON must never take every MCP server down with it.
    """
    servers: dict[str, dict] = {}
    for entry in scan_mcp_servers():
        if entry.error or entry.spec is None:
            logger.warning("MCP server {} ({}) skipped: {}", entry.name, entry.source, entry.error)
            continue
        servers[entry.name] = _resolve_placeholders(entry.spec, entry.here)
    return servers


def build_mcp_tools() -> list[BaseTool]:
    """Every MCP tool, one client PER server (error isolation at connect too).

    ``MultiServerMCPClient`` connects during ``get_tools()``; a server that
    refuses to start (bad command, missing binary) then fails alone — its
    tools are missing, the other servers' tools still load. The tool names
    (``mcp__<server>__<tool>``) are the client's, and the catalog files them
    under the ``mcp`` group unchanged.
    """
    servers = _load_config()
    if not servers:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    tools: list[BaseTool] = []
    for name, spec in servers.items():
        try:
            client = MultiServerMCPClient({name: spec})
            tools.extend(run_async(client.get_tools()))
        except Exception as exc:  # noqa: BLE001 — one dead server must not stop the rest
            logger.warning("MCP server {} failed to connect; its tools are skipped: {}", name, exc)
    return tools
