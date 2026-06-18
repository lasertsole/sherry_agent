"""Read file tool with project root restriction."""
from config import ROOT_DIR
from langchain_community.tools.file_management import ReadFileTool


def build_read_file_tool(session_id: str | None = None) -> ReadFileTool:
    tool = ReadFileTool(root_dir=str(ROOT_DIR))
    tool.handle_tool_error = True
    tool.name = "read_file"
    return tool

