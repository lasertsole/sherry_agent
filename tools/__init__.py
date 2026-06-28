from typing import Callable
from .mcp_plugin import build_mcp_tools
from langchain_core.tools import BaseTool
from .terminal import build_terminal_tool
from .subagent import build_subagent_tool
from .read_file import build_read_file_tool
from .write_file import build_write_file_tool
from .web_search import build_web_search_tool
from .python_repl import build_python_repl_tool
from .memory import build_memory_tool, memory_store
from .message_search import build_message_search_tool

def _flatten(builders: list, session_id: str | None) -> list[BaseTool]:
    """Call each builder; if result is a list, extend; otherwise append."""
    tools: list[BaseTool] = []
    for b in builders:
        result = b(session_id)
        if isinstance(result, list):
            tools.extend(result)
        else:
            tools.append(result)
    return tools

_MAIN_TOOLS_BUILDERS: list[Callable[[str | None], BaseTool]] = [
    build_python_repl_tool,
    build_read_file_tool,
    build_write_file_tool,
    build_memory_tool,
    build_message_search_tool,
    build_web_search_tool,
    build_terminal_tool,
    build_mcp_tools
]

def build_main_tools(session_id: str | None = None) -> list[BaseTool]:
    """Core tools + subagent"""
    return _flatten(_MAIN_TOOLS_BUILDERS, session_id)