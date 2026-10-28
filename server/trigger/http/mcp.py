"""MCP server discovery routes.

``GET /mcp/servers`` lists what the loader would pick up — bundled defaults
plus the user drop folder (`APP_HOME/mcp_servers/`), parsed in isolation per
entry so one bad JSON file shows as a ⚠ entry instead of a 500. The scan is
read-only: no MCP process is spawned, nothing connects (the real connection
happens per agent build in ``build_mcp_tools``), and a config change takes
effect on the next backend restart.
"""

from __future__ import annotations

from loguru import logger

from server.service import mcp_service
from server.trigger.core import app
from server.trigger.http.helpers import ok


@app.get("/mcp/servers")
async def list_mcp_servers_handler(request):  # noqa: ARG001 - Robyn's signature
    """The discovered MCP servers + the user drop folder's absolute path."""
    try:
        return ok({"success": True, **mcp_service.mcp_servers_snapshot()})
    except Exception:
        logger.exception("MCP server listing failed")
        return ok({"success": True, "servers": [], "user_dir": ""})
