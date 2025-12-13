"""write file tool with project root restriction."""

from __future__ import annotations
from config import ROOT_DIR
from langchain_community.tools.file_management import WriteFileTool


def build_write_file_tool(session_id: str | None = None) -> WriteFileTool:
    tool = WriteFileTool(root_dir=str(ROOT_DIR))
    tool.handle_tool_error = True
    tool.name = "write_file"
    return tool