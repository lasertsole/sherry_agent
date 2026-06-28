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

MAIN_TOOLS_BUILDERS: list[Callable[[str | None], BaseTool]] = [
    build_python_repl_tool,
    build_read_file_tool,
    build_write_file_tool,
    build_memory_tool,
    build_message_search_tool,
    build_web_search_tool,
    build_terminal_tool,
    build_mcp_tools,
]