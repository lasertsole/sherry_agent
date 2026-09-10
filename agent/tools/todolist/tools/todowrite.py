"""todowrite: full-replacement writer for the session todo list.

One behavior beyond the plain store echo lives here — the E6a fan-out reminder.
The FIRST ``todowrite`` call of each session appends ``_FANOUT_REMINDER`` to the
returned JSON so the model is forced to make an explicit delegation decision
(self vs subagent, and which category) instead of silently doing everything
inline. Later calls in the same session return pure JSON.

The reminder state is module-level and process-lifetime by design (one nudge per
session, not one per write); ``_reminded_sessions`` is a plain ``set`` the tests
replace wholesale for isolation.
"""

import json
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from .. import service

SessionId = Annotated[str, InjectedState("session_id")]

_FANOUT_REMINDER = """

[SYSTEM REMINDER] Consider whether any of these tasks should be delegated to subagents.
- Set delegation="subagent" for tasks that are independent with disjoint write scopes
- Set delegation="self" for interdependent or trivial tasks
- Declare dependencies with taskflow_run_task(..., depends_on=[...]); TaskFlow blocks
  and unlocks steps, then taskflow_dispatch batches the ready ones in parallel
- Route by category: quick|deep|ultrabrain|visual|git|writing
"""

_reminded_sessions: set[str] = set()


@tool("todowrite")
async def todowrite(
    todos: list[dict],
    session_id: SessionId = "",
) -> str:
    """Update the todo list for the current session (full replacement).

    Pass the COMPLETE list every time.
    Status: pending|in_progress|completed|cancelled.
    Priority: high|medium|low.
    Category (optional): quick|deep|ultrabrain|visual|git|writing.
    Delegation (optional): self|subagent.
    Subagent_id (optional): child_session_key returned by task tool.
    Plan_ref (optional): .omo/plans/*.md path.
    Flow_id (optional): TaskFlow flow id this todo tracks.
    Step_id (optional): TaskFlow step id (e.g. step-2) for DAG status.

    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_run_task(flow_id, task, depends_on=[...]); the blocked/ready/
    dispatched/done status and unlock-on-resume are owned by TaskFlow.
    """
    result = await service.TodoService.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER
    return output


__all__ = ["SessionId", "_FANOUT_REMINDER", "_reminded_sessions", "todowrite"]
