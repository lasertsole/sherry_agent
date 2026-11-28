"""MCP-style tool server (prototype ``src/mcp/server.py``).

Tools are exposed to Sherry as MCP tools, so ``build_main_tools()`` stays
untouched.  Worker calls are scoped to their own Ticket at the tool boundary.
"""

from __future__ import annotations

from typing import Any

from superagent.app.tools import HANDLERS, invoke
from superagent.app.tools.tool import read_tool, tools_for_role


class SuperAgentMCPServer:
    """Tool-list and dispatcher for main/worker roles."""

    def __init__(self, engine: Any, *, worker_tickets: dict[str, str] | None = None) -> None:
        self.engine = engine
        self.worker_tickets = worker_tickets or {}

    def tool_list(self, role: str) -> list[dict[str, Any]]:
        """Return tool descriptors filtered by role."""
        return [
            {"name": tool.name, "description": tool.description, "inputSchema": {"type": "object"}}
            for tool in tools_for_role(role)
        ]

    def call(
        self,
        role: str,
        name: str,
        args: dict[str, Any],
        *,
        session_key: str = "",
    ) -> Any:
        """Dispatch one tool call, enforcing worker Ticket scoping."""
        tool = read_tool(name)
        if tool is None or not tool.allowed(role):
            raise PermissionError(f"tool {name!r} is not available to {role}")
        if role == "worker":
            expected = self.worker_tickets.get(session_key)
            ticket_id = str(args.get("ticket_id", ""))
            if expected is not None and ticket_id != expected:
                raise PermissionError("worker may only act on its own Ticket")
        if tool.handler not in HANDLERS:
            raise KeyError(tool.handler)
        return invoke(self.engine, name, args)


__all__ = ["SuperAgentMCPServer"]
