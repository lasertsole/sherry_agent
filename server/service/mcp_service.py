"""MCP discovery service: the read-only view behind ``GET /mcp/servers``.

The scan itself lives in ``agent/tools/mcp_plugin.py`` (the loader shares it
— one discovery pass, two consumers). This service is the server-side shape:
the UI's list (name / source / command / error) plus the drop folder's path
so the extend panel's "open folder" button can aim at it.

Design constraints: the scan is pure discovery — no MCP process is spawned,
nothing connects. Real connections happen once per agent build inside
``build_mcp_tools()`` (one client per server), and a config change takes
effect on the next backend restart (the agent graph holds the tool list).
"""

from __future__ import annotations

from typing import Any

from agent.tools.mcp_plugin import scan_mcp_servers
from config import MCP_SERVERS_DIR

__all__ = ["mcp_servers_snapshot"]


def mcp_servers_snapshot() -> dict[str, Any]:
    """The discovered MCP servers + the user drop folder's absolute path.

    ``has_error`` marks an entry whose JSON could not be parsed (the loader
    skips it; the UI shows it with a warning mark so the user can fix the
    file). Everything else carries its raw spec's command for the list view.
    """
    servers = []
    for entry in scan_mcp_servers():
        command = None
        if entry.spec is not None:
            command = entry.spec.get("command")
        servers.append(
            {
                "name": entry.name,
                "source": entry.source,
                "command": command,
                "has_error": entry.error is not None,
                "error": entry.error,
            }
        )
    return {"servers": servers, "user_dir": str(MCP_SERVERS_DIR)}
