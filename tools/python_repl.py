"""Python REPL tool."""
from langchain_experimental.tools import PythonREPLTool


def build_python_repl_tool(session_id: str | None = None) -> PythonREPLTool:
    tool = PythonREPLTool()
    tool.name = "python_repl"
    tool.handle_tool_error = True
    return tool

