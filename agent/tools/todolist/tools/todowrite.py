"""todowrite: full replacement + local, todo_id-addressed mutations.

Every todo carries a stable per-session ``todo_id`` (rendered as ``#N`` in the
system reminder). The default ``replace`` action keeps the old full-list
contract; ``patch`` / ``add`` / ``remove`` / ``reattach`` change one item
without rewriting the list, so a linked ``flow_id`` survives. ``list_orphans``
exposes active flows whose GC-root todo was lost, and ``reattach`` binds one
back.

One behavior beyond the plain store echo lives here — the fan-out reminder.
The FIRST mutating ``todowrite`` call of each session appends
``_FANOUT_REMINDER`` to the returned JSON so the model is forced to make an
explicit delegation decision (self vs subagent, and which category) instead of
silently doing everything inline. Later calls in the same session return pure
JSON.

The reminder state is module-level and process-lifetime by design (one nudge per
session, not one per write); ``_reminded_sessions`` is a plain ``set`` the tests
replace wholesale for isolation.
"""

import json
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .. import service
from agent.tools.pub_base import SessionId


_FANOUT_REMINDER = """

[SYSTEM REMINDER] Consider whether any of these tasks should be delegated to subagents.
- Set delegation="subagent" for tasks that are independent with disjoint write scopes
- Set delegation="self" for interdependent or trivial tasks
- Declare dependencies with taskflow_step(action='run_task', ..., depends_on=[...]); TaskFlow blocks
  and unlocks steps, then taskflow_step(action='dispatch') batches the ready ones in parallel
- Route by category: quick|deep|ultrabrain|visual|git|writing

Prefer local actions (patch/add/remove) over rewriting the whole list: they keep
each todo's todo_id and its TaskFlow flow_id link intact.
"""

_reminded_sessions: set[str] = set()

_TODO_ICONS = {
    "pending": "○",
    "in_progress": "▶",
    "completed": "✓",
    "cancelled": "✕",
}


class TodoWriteInput(BaseModel):
    """Full-replacement or local todo mutation. Only the action's fields are used."""

    action: Literal["replace", "patch", "add", "remove", "reattach", "list_orphans"] = Field(
        default="replace",
        description=(
            "replace (default): pass the COMPLETE todos list. "
            "patch: change one todo by todo_id. "
            "add: append one todo, or insert after after_todo_id. "
            "remove: delete one todo by todo_id. "
            "reattach: bind a todo_id to an active flow_id as its GC root. "
            "list_orphans: list active flows with no active todo root."
        ),
    )
    todos: list[dict] | None = Field(
        default=None,
        description=(
            "action=replace only: the COMPLETE list. Every item needs content; optional "
            "status, priority, category, delegation, subagent_id, plan_ref, flow_id, step_id. "
            "todo_id is assigned by the store and ignored here."
        ),
    )
    todo_id: int | None = Field(
        default=None,
        description="Stable todo id from the #N marker (required for patch/remove/reattach).",
    )
    after_todo_id: int | None = Field(
        default=None,
        description=(
            "action=add only: None appends at the tail; a todo_id inserts the new item "
            "immediately after that todo."
        ),
    )
    content: str | None = Field(
        default=None, description="Todo text (required for add; patch field)."
    )
    status: str | None = Field(
        default=None, description="patch field: pending|in_progress|completed|cancelled."
    )
    priority: str | None = Field(default=None, description="patch field: high|medium|low.")
    category: str | None = Field(
        default=None, description="add/patch field: quick|deep|ultrabrain|visual|git|writing."
    )
    plan_ref: str | None = Field(default=None, description="add field: session plan file path.")
    flow_id: str | None = Field(
        default=None, description="reattach field: TaskFlow flow id to bind as the GC root."
    )
    session_id: SessionId = ""


def _format_id_error(error: service.TodoIdNotFoundError) -> str:
    lines = [
        f"{error.field} {error.todo_id} not found in this session's todolist. "
        "It may have been removed or never existed.",
        "Current todos (use one of these todo_id values):",
    ]
    if not error.valid_todos:
        lines.append("- (no todos)")
    for todo in error.valid_todos:
        icon = _TODO_ICONS.get(str(todo.get("status") or ""), "○")
        priority = todo.get("priority") or "medium"
        lines.append(f"- [{icon}] #{todo.get('todo_id')} {todo.get('content', '')} ({priority})")
    return "\n".join(lines)


async def _run_todo_action(
    action: Literal["replace", "patch", "add", "remove", "reattach", "list_orphans"] = "replace",
    todos: list[dict] | None = None,
    todo_id: int | None = None,
    after_todo_id: int | None = None,
    content: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    plan_ref: str | None = None,
    flow_id: str | None = None,
    session_id: str = "",
) -> str:
    """Execute one todo action and return JSON text WITHOUT the fan-out reminder."""

    async def _run() -> object:
        if action == "replace":
            if todos is None:
                raise ValueError(
                    "action='replace' requires todos (the COMPLETE list); "
                    "use add/patch/remove for a local change"
                )
            items = list(todos)
            problems = [
                f"item #{index} needs a non-empty 'content' string"
                for index, item in enumerate(items)
                if not isinstance(item, dict) or not str(item.get("content") or "").strip()
            ]
            if problems:
                raise ValueError(
                    "todowrite refused: "
                    + "; ".join(problems)
                    + '. Pass the COMPLETE list again, every item as {"content": "...", '
                    '"status": "pending|in_progress|completed|cancelled", ...}.'
                )
            return await service.TodoService.update_todos(session_id, items, plan_ref=plan_ref)

        if action == "list_orphans":
            return await service.TodoService.list_orphan_flows(session_id)

        if action == "add":
            if not content or not str(content).strip():
                raise ValueError("action='add' requires a non-empty content")
            return await service.TodoService.add_todo(
                session_id,
                content=content,
                category=category,
                after_todo_id=after_todo_id,
                plan_ref=plan_ref,
            )

        if todo_id is None:
            raise ValueError(f"action='{action}' requires todo_id (the #N marker)")

        if action == "patch":
            if all(value is None for value in (content, status, priority, category)):
                raise ValueError(
                    "action='patch' requires at least one of content/status/priority/category"
                )
            return await service.TodoService.patch_todo(
                session_id,
                todo_id,
                content=content,
                status=status,
                priority=priority,
                category=category,
            )

        if action == "remove":
            return await service.TodoService.remove_todo(session_id, todo_id)

        # action == "reattach"
        if not flow_id:
            raise ValueError("action='reattach' requires flow_id")
        try:
            return await service.TodoService.reattach_todo(session_id, todo_id, flow_id)
        except service.TodoStoreError as error:
            raise ValueError(str(error)) from error

    try:
        result = await _run()
    except service.TodoIdNotFoundError as error:
        return _format_id_error(error)
    except service.TodoReferenceError as error:
        return f"todowrite refused: {error}"
    except ValueError as error:
        return f"todowrite refused: {error}"
    return json.dumps(result, ensure_ascii=False, indent=2)


@tool("todowrite", args_schema=TodoWriteInput)
async def todowrite(
    action: Literal["replace", "patch", "add", "remove", "reattach", "list_orphans"] = "replace",
    todos: list[dict] | None = None,
    todo_id: int | None = None,
    after_todo_id: int | None = None,
    content: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    plan_ref: str | None = None,
    flow_id: str | None = None,
    session_id: SessionId = "",
) -> str:
    """Update the todo list for the current session.

    Every todo has a stable ``todo_id`` shown as ``#N`` in the system reminder
    and in todoread/todowrite output. Prefer the local actions over rewriting
    the whole list: they preserve the todo's identity and its flow_id link.

    Actions:
      - replace (default): pass the COMPLETE todos list.
      - patch: todo_id + any of content/status/priority/category.
      - add: content; after_todo_id=None appends, #N inserts after that todo.
      - remove: todo_id.
      - reattach: todo_id + flow_id (bind an existing todo as a flow's GC root).
      - list_orphans: list active flows with no active todo root.

    Status: pending|in_progress|completed|cancelled.
    Priority: high|medium|low.
    Category (optional): quick|deep|ultrabrain|visual|git|writing.
    Delegation (optional): self|subagent.
    Subagent_id (optional): child_session_key returned by sessions_spawn.
    Plan_ref (optional): plan file path — session-scoped
    workspace/sessions/<session_id>/plans/*.md.
    Flow_id (optional): TaskFlow flow id this todo tracks.
    Step_id (optional): TaskFlow step id (e.g. step-2) for DAG status.

    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_step(action='run_task', flow_id=..., task=..., depends_on=[...]); the blocked/ready/
    dispatched/done status and unlock-on-resume are owned by TaskFlow.
    """
    output = await _run_todo_action(
        action=action,
        todos=todos,
        todo_id=todo_id,
        after_todo_id=after_todo_id,
        content=content,
        status=status,
        priority=priority,
        category=category,
        plan_ref=plan_ref,
        flow_id=flow_id,
        session_id=session_id,
    )
    if action == "replace":
        warning = service.TodoService.consume_reference_warning(session_id)
        if warning:
            output += (
                "\n\n[REFERENCE INTEGRITY] The replacement dropped the GC-root todo of "
                f"active flow(s) {', '.join(warning)}; those flow(s) were auto-cancelled. "
                "If that was unintended, re-create the flow or use local actions "
                "(patch/add/remove) to keep their todo."
            )
    if action != "list_orphans" and session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER
    return output


__all__ = [
    "SessionId",
    "TodoWriteInput",
    "_FANOUT_REMINDER",
    "_reminded_sessions",
    "_run_todo_action",
    "todowrite",
]
