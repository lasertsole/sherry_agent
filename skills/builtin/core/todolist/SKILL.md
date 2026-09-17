---
name: todolist
description: Session-scoped task tracking with delegation routing and TaskFlow-backed DAG execution. Use for 3+ step work or any multi-item request.
scope: main_only
---

# TodoList: Session-scoped task tracking

The session-scoped lightweight planning/checklist layer. It is responsible for "thinking it through, keeping watch, and driving it to completion"; the actual cross-session DAG execution, dependency unlocking, and parallel dispatch are handled by TaskFlow.

## When to use

- When a complex job arrives with 3+ steps, build a todo list before starting.
- When a request contains multiple independent items, split it into atomic todos.
- Use it also when the scope is unclear: writing it down is what makes the boundaries visible.
- Update the status after each completed step; once everything is done, clear the list with an empty list `[]`.

## Tools

- `todowrite(todos)`: **fully replaces** the current session's todo list. Always pass the complete list, not an incremental patch.
- `todoread()`: read the current list back from the database. When unsure of the current state, read before writing.

## Fields

- `content`: the text of a single todo. Recommended format `[WHERE] [HOW] to [WHY] - expect [RESULT]`; one todo should be a single atomic action completable within 1-3 tool calls.
- `status`: `pending | in_progress | completed | cancelled`, default `pending`.
- `priority`: `high | medium | low`, default `medium`.
- `category` (optional): `quick | deep | ultrabrain | visual | git | writing`, the delegation routing decision.
- `delegation` (optional): `self | subagent`, whether to do it yourself or delegate it to a subagent.
- `subagent_id` (optional): filled with the subagent's `child_session_key` after it is spawned.
- `plan_ref` (optional): the associated plan file path — session-scoped `workspace/sessions/<session_id>/plans/*.md` (legacy `.omo/plans/*.md` is still accepted).
- `flow_id` (optional): the associated TaskFlow flow id.
- `step_id` (optional): the associated TaskFlow step id (e.g. `step-2`).

## Rules

- Every `todowrite` passes the complete list; never try to change only one line.
- **Only one `in_progress` at a time.** Until the current item is complete, do not set the next item to in progress.
- **Only mark `completed` after verification passes.** Run the acceptance criteria first, then change the status; if verification fails, leave it incomplete or switch it to `cancelled` and re-plan.
- A todo with `delegation: subagent` must not be marked `completed` before the subagent returns.
- Marking many items complete in one batch hides the real progress; mark them one by one.
- Once a completed status is written, the continuation system in later turns uses it to decide whether to proceed.

## DAG / dependencies / waves are not here

The todolist layer does **not** track `depends_on`, does **not** store waves, does **not** cache step status, and does **not** recompute the frontier. All of that belongs to TaskFlow:

- Declare dependencies: `taskflow_run_task(flow_id, task, depends_on=["step-1"])`. While dependencies are unmet, the step is recorded as `blocked` and is not dispatched.
- Dispatch ready steps in parallel: `taskflow_dispatch(flow_id, step_ids)`.
- Wait for the child sessions dispatched by this flow to settle: `taskflow_wait_all(flow_id, ...)`.
- Inject results and unlock successors: `taskflow_resume(flow_id, child_session_key, result)`. Resume only unlocks; it does not dispatch automatically.
- Read back DAG state: `taskflow_summary(flow_id)` renders each step's `status` (`blocked | ready | dispatched | done`) and `depends_on`.

A todo points at the corresponding flow step only through the optional `flow_id` / `step_id`; when DAG state is needed, read it back with `taskflow_summary` — do not rebuild a scheduler at the todo layer. The dependency graph, unlocking, and parallel dispatch have exactly one authoritative source: TaskFlow.
