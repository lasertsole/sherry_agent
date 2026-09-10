"""TodoList tool family: ``todowrite`` + ``todoread``.

``build_todolist_tools`` mirrors ``build_taskflow_tools``: it tags every tool
with ``handle_tool_error=True`` (business errors become readable text) and
``metadata={"scope": "main_only"}`` (the subagent tool-policy drops the family,
which matches the todolist skill's scope). It additionally injects the E2
``_TODOWRITE_FORMAT_RULES`` block into the ``todowrite`` description — the
description is part of the prompt, so the format/granularity/delegation rules
reach the model on every turn without a separate system-prompt edit.

The append is idempotent (guarded by a substring check) because
``build_main_tools`` may be called more than once per process.
"""

from langchain_core.tools import BaseTool

from .todowrite import _FANOUT_REMINDER, todowrite
from .todoread import todoread

_TODOWRITE_FORMAT_RULES = """

## Todo Format (MANDATORY)

Each todo title MUST encode four elements: WHERE, WHY, HOW, and EXPECTED RESULT.
Format: "[WHERE] [HOW] to [WHY] - expect [RESULT]"

## Granularity Rules
Each todo MUST be a single atomic action completable in 1-3 tool calls.
**Size test**: Can you complete this todo by editing one file or running one command?
If not, it's too big — split it.

## Orchestrator Rules (MANDATORY)
- One in_progress at a time. Complete it before starting the next.
- Mark completed immediately after finishing each item.
- delegation="subagent" todos: spawn subagent FIRST, then set subagent_id.
- Do NOT mark completed while subagent is still running (Transition Barrier).
- Do NOT mark completed without running acceptance criteria (Sisyphus).

## Delegation Fields (optional but recommended)
- category: quick|deep|ultrabrain|visual|git|writing — routing verdict for subagent dispatch
- delegation: self|subagent — whether this todo should be delegated
- subagent_id: set after dispatching a subagent (use the child_session_key returned by task tool)

## DAG / TaskFlow Fields (optional)
- plan_ref: .omo/plans/*.md path
- flow_id: linked TaskFlow flow id (DAG lives in TaskFlow, not in todos.db)
- step_id: linked TaskFlow step id (e.g. step-2); read its status via taskflow_summary
- To declare dependencies, call taskflow_run_task(..., depends_on=[...]); do NOT
  re-implement wave/frontier scheduling in the todolist layer
"""

_TODOLIST_TOOLS: list[BaseTool] = [todowrite, todoread]


def build_todolist_tools() -> list[BaseTool]:
    """Build and return the todowrite/todoread tools for main-agent wiring."""
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    if _TODOWRITE_FORMAT_RULES not in todowrite.description:
        todowrite.description += _TODOWRITE_FORMAT_RULES
    return list(_TODOLIST_TOOLS)


__all__ = [
    "_FANOUT_REMINDER",
    "_TODOLIST_TOOLS",
    "_TODOWRITE_FORMAT_RULES",
    "build_todolist_tools",
    "todowrite",
    "todoread",
]
