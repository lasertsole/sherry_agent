from typing import Callable
from langchain_core.tools import BaseTool
from .terminal import build_terminal_tool
from .subagent import build_subagent_tool
from .read_file import build_read_file_tool
from .write_file import build_write_file_tool
from .web_search import build_web_search_tool
from .python_repl import build_python_repl_tool
from .memory import build_memory_tool, memory_store
from .message_search import build_message_search_tool

CORE_TOOLS_BUILDERS: list[Callable[[str | None], BaseTool]] = [
    build_python_repl_tool,
    build_read_file_tool,
    build_write_file_tool,
]

MAIN_TOOLS_BUILDERS: list[Callable[[str | None], BaseTool]] = [
    *CORE_TOOLS_BUILDERS,
    build_subagent_tool
]

ALL_TOOLS_BUILDERS = [
    *MAIN_TOOLS_BUILDERS,
    build_memory_tool,
    build_message_search_tool,
    build_web_search_tool,
    build_terminal_tool
]

def build_core_tools(session_id: str | None = None)-> list[BaseTool]:
    """核心工具"""
    return [builder(session_id) for builder in CORE_TOOLS_BUILDERS]

def build_main_tools(session_id: str | None = None)-> list[BaseTool]:
    """主要工具"""
    return [builder(session_id) for builder in MAIN_TOOLS_BUILDERS]

def build_all_tools(session_id: str | None = None)-> list[BaseTool]:
    """全部工具"""
    return [builder(session_id) for builder in ALL_TOOLS_BUILDERS]