from langchain_core.tools import BaseTool
from .file_tools import *
from .skill_tools import *
from typing import Callable
from .mcp_plugin import build_mcp_tools
from .terminal import build_terminal_tool
from .subagent import build_subagent_tool
from .web_search import build_web_search_tool
from .python_repl import build_python_repl_tool
from .memory import build_memory_tool, memory_store
from .message_search import build_message_search_tool

def tool_flatten(builders: list[Callable[[], BaseTool]]) -> list[BaseTool]:
    """Call each builder; if result is a list, extend; otherwise append."""
    tools: list[BaseTool] = []
    for b in builders:
        result = b()
        if isinstance(result, list):
            tools.extend(result)
        else:
            tools.append(result)
    return tools

_MAIN_TOOLS_BUILDERS: list[Callable[[], BaseTool]] = [
    build_python_repl_tool,
    build_read_file_tool,
    build_write_file_tool,
    build_patch_file_tool,
    build_memory_tool,
    build_web_search_tool,
    build_terminal_tool,
    build_mcp_tools,
    build_skill_manage_tool,
    build_skill_list_tool,
    build_skill_view_tool,
    build_message_search_tool
]

def build_main_tools() -> list[BaseTool]:
    """Core tools + subagent"""
    return tool_flatten(_MAIN_TOOLS_BUILDERS)