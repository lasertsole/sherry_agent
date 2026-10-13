"""Query tools for background terminal tasks.

``terminal(run_in_background=True)`` returns a task id; these two tools are how
the agent asks about it afterwards: list this session's tasks, and read one
task's output (a bounded TAIL — a long-running command's status lives at the
end of its log). Both are main-agent tools: a child has no business managing the
parent's background processes.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from agent.tools.pub_base import SessionId
from agent.tools.terminal_bg_registry import (
    background_registry,
    read_output_tail,
)

__all__ = ["build_terminal_bg_list_tool", "build_terminal_bg_output_tool"]


@tool("terminal_bg_list")
def terminal_bg_list(session_id: SessionId = "") -> str:
    """List this session's background terminal tasks (newest first).

    Returns one JSON object per task with its id, command, status
    (running / completed / failed / killed), pid and exit code. Use the task id
    with terminal_bg_output to read what the command printed.
    """
    tasks = background_registry.list_session(str(session_id or ""))
    if not tasks:
        return "No background terminal tasks for this session."
    return json.dumps(
        [
            {
                "task_id": task.task_id,
                "command": task.command,
                "status": task.status,
                "pid": task.pid,
                "exit_code": task.exit_code,
            }
            for task in tasks
        ],
        ensure_ascii=False,
    )


@tool("terminal_bg_output")
def terminal_bg_output(task_id: str, session_id: SessionId = "") -> str:
    """Read the tail of a background terminal task's output.

    Args:
        task_id: The id from terminal(run_in_background=True).
    """
    task = background_registry.get(str(task_id or "").strip())
    if task is None or task.session_id != str(session_id or ""):
        return f"No background task found with task_id: {task_id}"
    if task.output_path is None:
        return "This background task has no output file."
    text = read_output_tail(task.output_path)
    if not text.strip():
        return f"(no output yet; status={task.status})"
    return text


def build_terminal_bg_list_tool() -> object:
    from langchain_core.tools import BaseTool

    tool_obj: BaseTool = terminal_bg_list
    tool_obj.handle_tool_error = True
    # Main-agent only: the child-agent tool policy drops a ``main_only`` tool
    # before its allow/deny gate — a subagent must not manage the parent's
    # background processes.
    tool_obj.metadata = {**(tool_obj.metadata or {}), "scope": "main_only"}
    return tool_obj


def build_terminal_bg_output_tool() -> object:
    from langchain_core.tools import BaseTool

    tool_obj: BaseTool = terminal_bg_output
    tool_obj.handle_tool_error = True
    tool_obj.metadata = {**(tool_obj.metadata or {}), "scope": "main_only"}
    return tool_obj
