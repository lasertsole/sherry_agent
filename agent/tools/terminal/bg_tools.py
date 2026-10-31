"""Query tools for background terminal tasks.

``terminal(run_in_background=True)`` returns a task id; these tools are how the
agent asks about it afterwards: list this session's tasks, or read one task's
output (a bounded TAIL — a long-running command's status lives at the end of its
log).

The model-facing surface is one ``terminal_bg`` tool whose action selects
``list`` or ``output``. The original ``terminal_bg_list`` / ``terminal_bg_output``
functions stay as the handlers (and for tests/evals via
``build_legacy_terminal_bg_tools``). Both actions are main-agent-only: a child
has no business managing the parent's background processes.
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, tool

from agent.tools.pub_base import SessionId
from .bg_registry import (
    background_registry,
    read_output_tail,
)

__all__ = [
    "build_legacy_terminal_bg_tools",
    "build_terminal_bg_list_tool",
    "build_terminal_bg_output_tool",
    "build_terminal_bg_tool",
    "terminal_bg",
    "terminal_bg_list",
    "terminal_bg_output",
]


def _list_session_tasks(session_id: str) -> str:
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


def _kill_session_task(task_id: str | None, session_id: str) -> str:
    """Kill one background task of THIS session (the agent's own cancel path)."""
    resolved = str(task_id or "").strip()
    if not resolved:
        return "Error: terminal_bg action='kill': missing required parameter task_id."
    task = background_registry.get(resolved)
    if task is None or task.session_id != str(session_id or ""):
        return f"No background task found with task_id: {task_id}"
    if not background_registry.kill_task(resolved, str(session_id or "")):
        return f"Background task {resolved} is not running (status={task.status}); nothing to stop."
    return (
        f"Background task {resolved} stopped (process tree killed). Its exit notification follows."
    )


def _read_task_output(task_id: str | None, session_id: str) -> str:
    task = background_registry.get(str(task_id or "").strip())
    if task is None or task.session_id != str(session_id or ""):
        return f"No background task found with task_id: {task_id}"
    if task.output_path is None:
        return "This background task has no output file."
    text = read_output_tail(task.output_path)
    if not text.strip():
        return f"(no output yet; status={task.status})"
    return text


@tool("terminal_bg_list")
def terminal_bg_list(session_id: SessionId = "") -> str:
    """List this session's background terminal tasks (newest first).

    Returns one JSON object per task with its id, command, status
    (running / completed / failed / killed), pid and exit code. Use the task id
    with ``terminal_bg(action="output", task_id=...)`` to read what it printed.
    """
    return _list_session_tasks(str(session_id or ""))


@tool("terminal_bg_output")
def terminal_bg_output(task_id: str, session_id: SessionId = "") -> str:
    """Read the tail of a background terminal task's output.

    Args:
        task_id: The id from terminal(run_in_background=True).
    """
    return _read_task_output(task_id, str(session_id or ""))


@tool("terminal_bg")
def terminal_bg(
    action: str,
    task_id: str | None = None,
    session_id: SessionId = "",
) -> str:
    """List this session's background terminal tasks, read one task's tail output, or stop one.

    action="list" — list this session's tasks (no required fields).
    action="output" — read the tail of one task (requires task_id).
    action="kill" — stop ONE running task's process tree (requires task_id). The
    current turn and the session's other background tasks are unaffected.
    """
    normalized = str(action or "").strip().lower()
    if normalized == "list":
        return _list_session_tasks(str(session_id or ""))
    if normalized == "output":
        if not str(task_id or "").strip():
            return (
                "Error: terminal_bg action='output': missing required parameter(s): "
                "task_id (the id from terminal(run_in_background=True))."
            )
        return _read_task_output(task_id, str(session_id or ""))
    if normalized == "kill":
        return _kill_session_task(task_id, str(session_id or ""))
    return f"Error: terminal_bg has unknown action '{action}'. Available: list, output, kill."


def _tag_main_only(tool_obj: BaseTool) -> BaseTool:
    tool_obj.handle_tool_error = True
    # Main-agent only: the child-agent tool policy drops a ``main_only`` tool
    # before its allow/deny gate — a subagent must not manage the parent's
    # background processes.
    tool_obj.metadata = {**(tool_obj.metadata or {}), "scope": "main_only"}
    return tool_obj


def build_terminal_bg_tool() -> BaseTool:
    """The model-facing merged terminal_bg tool."""
    return _tag_main_only(terminal_bg)


def build_terminal_bg_list_tool() -> BaseTool:
    """Legacy handler builder (not registered in the model face)."""
    return _tag_main_only(terminal_bg_list)


def build_terminal_bg_output_tool() -> BaseTool:
    """Legacy handler builder (not registered in the model face)."""
    return _tag_main_only(terminal_bg_output)


def build_legacy_terminal_bg_tools() -> list[BaseTool]:
    """The two pre-merge tools for tests/evals and A/B comparisons."""
    return [build_terminal_bg_list_tool(), build_terminal_bg_output_tool()]
