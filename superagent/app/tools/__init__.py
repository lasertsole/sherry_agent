"""Orchestrator tool backends exposed to Sherry via MCP."""

from __future__ import annotations

from superagent.app.tools.executor import HANDLERS as WORKER_HANDLERS
from superagent.app.tools.main import HANDLERS as MAIN_HANDLERS
from superagent.app.tools.tool import ALL_TOOLS, Tool, read_tool, tools_for_role

HANDLERS = {**MAIN_HANDLERS, **WORKER_HANDLERS}


def invoke(engine: object, tool_name: str, args: dict[str, object]) -> object:
    """Invoke one orchestrator tool by name."""
    tool = read_tool(tool_name)
    if tool is None:
        raise KeyError(tool_name)
    handler = HANDLERS.get(tool.handler)
    if handler is None:
        raise KeyError(tool.handler)
    return handler(engine, args)


__all__ = ["ALL_TOOLS", "HANDLERS", "Tool", "invoke", "read_tool", "tools_for_role"]
