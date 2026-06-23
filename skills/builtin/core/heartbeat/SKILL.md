---
name: heartbeat
description: 'Periodic wake-up service that checks HEARTBEAT.md for pending tasks and executes them automatically. Use this skill to write or update HEARTBEAT.md.'
---

## How to write HEARTBEAT.md

HEARTBEAT.md is the task manifest for the heartbeat service. Every tick, the service reads this file and lets the LLM decide whether there are active tasks to execute.

### Add a pending task
```python
from skills.builtin.core.heartbeat.scripts import add_task_to_heartbeat

task_text: str = "{placeholder}" # <- The task description to add (as a Markdown list item or paragraph)
index: int | None = int("{placeholder}") # <- Optional 0-based insertion position within Active Tasks content lines (skipping blanks and HTML comments). ``None`` (default) appends at end.

res = add_task_to_heartbeat(task_text, index)
print(res)
```

Example values for `task_text`:
```markdown
- Check email inbox, if there are unread emails from Alice, summarize and report
- If last code sync was more than 24 hours ago, pull latest main branch and run tests
- Send daily work report at 18:00: summarize today's conversation records
```

### List active tasks
```python
from skills.builtin.core.heartbeat.scripts import list_active_tasks
print(list_active_tasks())
```

### List completed tasks
```python
from skills.builtin.core.heartbeat.scripts import list_completed_tasks
print(list_completed_tasks())
```

### Move a finished task to completed
Use this when a task is done and won't be executed again — it moves the task from Active Tasks to Completed.
```python
from skills.builtin.core.heartbeat.scripts import move_task_to_completed
task_text: str = "{placeholder}" # <- The task description to add (as a Markdown list item or paragraph)
res = move_task_to_completed(task_text)
print(res)
```

### Remove task(s) from Completed
Both functions accept `None | str | list[str]` — the behavior is determined by the argument type:

- `None` → clear **all** completed records
- `str` → substring match, remove matching line(s)
- `list[str]` → substring match each string, remove all matching

```python
from skills.builtin.core.heartbeat.scripts import remove_tasks_from_completed

# Clear all completed records
res = remove_tasks_from_completed()
print(res)

# Remove any line containing "check email"
res = remove_tasks_from_completed("check email")
print(res)

# Remove multiple by substring match
res = remove_tasks_from_completed(["check email", "pull code", "daily report"])
print(res)
```

`clear_completed_tasks` is an alias with identical behavior:

```python
from skills.builtin.core.heartbeat.scripts import clear_completed_tasks
# all
res = clear_completed_tasks()
print(res)

# single substring
res = clear_completed_tasks("check email")
print(res)

# batch
clear_completed_tasks(["pull code", "report"])
print(res)
```