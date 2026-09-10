"""todoread: explicit reader for the session todo list.

The system prompt already carries the current list (compression-proof, rebuilt
from the store each turn), so this tool exists for the cases where the model
cannot trust that view: after a long tool sequence, after a compaction, or to
re-read the exact persisted state before marking something complete.
"""

import json
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from .. import service

SessionId = Annotated[str, InjectedState("session_id")]

_NO_TODOS = "No todos found."


@tool("todoread")
async def todoread(
    session_id: SessionId = "",
) -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.TodoService.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else _NO_TODOS


__all__ = ["SessionId", "todoread"]
