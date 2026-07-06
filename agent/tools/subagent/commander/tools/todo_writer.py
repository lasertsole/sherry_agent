from typing import Annotated
from pathlib import Path
from config import SESSIONS_DIR
from pydantic import BaseModel, Field
from langchain.tools import tool
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import InjectedState

class TodoArgs(BaseModel):
    file_content: str = Field(..., description="File content")


def build_todo_writer_tool() -> BaseTool:
    """Build the todo_writer @tool with task_id closed over and error handling enabled."""

    @tool(args_schema=TodoArgs, infer_schema=False)
    async def write_todo(
        file_content: str,
        master_session_id: Annotated[str, InjectedState("master_session_id")] = "",
        task_id: Annotated[str, InjectedState("task_id")] = "",
    ) -> None:
        """Write todo list to file."""
        todo_dir: Path = SESSIONS_DIR / master_session_id / "todo"
        todo_dir.mkdir(parents=True, exist_ok=True)
        file_path: Path = todo_dir / f"{task_id}.md"
        file_path.write_text(file_content, encoding="utf-8")

    write_todo.handle_tool_error = True
    return write_todo