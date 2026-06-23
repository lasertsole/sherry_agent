from typing import Any
from pathlib import Path
from config import SESSIONS_DIR
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool

class TodoArgs(BaseModel):
    file_content: str = Field(..., description="File content")

class TodoWriter(BaseTool):
    name: str = "write_todo"
    description: str = "Write todo list to file"
    args_schema: type[BaseModel] = TodoArgs

    def __init__(self, session_id: str, task_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id: str = session_id
        self._task_id: str = task_id

        todo_dir: Path = SESSIONS_DIR / session_id / "todo"
        # Ensure the directory exists
        todo_dir.mkdir(parents=True, exist_ok=True)

        self._file_path: Path = todo_dir / f"{self._task_id}.md"

    def _run(self, file_content: str, **kwargs: Any) -> None:
        with open(self._file_path, 'w', encoding='utf-8') as f:
            f.write(file_content)

    async def _arun(self, file_content: str, **kwargs: Any) -> None:
        self._run(file_content, **kwargs)

def build_todo_writer_tool(session_id: str, task_id: str) -> TodoWriter:
    tool: TodoWriter = TodoWriter(session_id, task_id)
    tool.handle_tool_error = True
    return tool