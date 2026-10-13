# 📦 TodoList Data Layer — Plans, Boulder, Ledger & todos.db

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [TodoList](../README.md): the session-scoped data stores behind the planning layer — plan files, boulder state, the evidence ledger, and the todos.db schema.

---

## Data Layer

### Plan Files (workspace/sessions/<session_id>/plans/*.md)

Checkbox-format Markdown defining the complete HTN decomposition. Plans are
session-scoped: **clearing a session deletes that session's plans** (the whole
`workspace/sessions/<session_id>/` tree is removed). A plan reference resolves
through `config.path.resolve_plan_path` to the session tree or an explicit
repo-relative path; no external orchestration directory participates.

```markdown
# <Plan Name>

## Goal

<detailed goal: plan name, path, terminal state, delivery mode, verification>

## Context

<project background, constraints, known info>

## TODOs

### Wave 0: <Wave description>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>

### Wave 1: <Wave description> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder State (src/data/boulder.json)

Persistent work state:

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": "workspace/sessions/<session_id>/plans/<plan-name>.md",
      "plan_name": "<plan-name>",
      "session_ids": ["sherry:<session_id>"],
      "status": "active",
      "worktree_path": null,
      "created_at": "2026-09-07T00:00:00Z"
    }
  }
}
```

### Evidence Ledger (src/data/evidence-ledger.jsonl)

One JSON object per line, recording execution evidence per checkbox:

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "artifact": "src/data/evidence/xxx.txt", "adversarial_classes": {"stale_state": "not-applicable", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
{"event": "stale", "file_path": "agent/tools/taskflow/step_judge.py", "session_id": "sherry:xxx", "timestamp": "..."}
```

- **Auto-recording**: `agent/tools/todolist/evidence_recorder.py` always appends a row for verification commands recognized in `terminal` / `python_repl` results (`kind` + `status`; the taxonomy lives in `EVIDENCE_LEDGER["verify_commands"]`) and a `stale` event after `write_file` / `patch_file` edits. Every entry point is fail-open — a ledger write never breaks the tool.
- **Staleness is derived**: an evidence row is stale iff a later `{"event": "stale"}` row names a path contained in its `command` (`agent/tools/taskflow/evidence_collector.py`), so the historical lines are never rewritten.
- **Session views**: `EvidenceLedger.for_session(session_key)` / `read_for_session()` filter the shared file by `session_id`; the file itself stays repo-wide.

### todos.db — Session-Level TODO Storage

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    plan_ref     TEXT    DEFAULT NULL,
    flow_id      TEXT    DEFAULT NULL,
    step_id      TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
```

| Field      | Description                                                        |
| ---------- | ------------------------------------------------------------------ |
| `plan_ref` | Linked plan file path — session-scoped `workspace/sessions/<session_id>/plans/*.md` |
| `flow_id`  | Linked TaskFlow flow id (DAG owned by TaskFlow)                    |
| `step_id`  | Linked TaskFlow step id (e.g. `step-2`), for re-reading DAG status |

> **Design boundary**: `todos.db` does **not** hold `depends_on` / waves / step status. The dependency graph, `blocked/ready/dispatched/done` and unlock logic are all provided by TaskFlow. The todo only points to the corresponding flow step via `flow_id`/`step_id`; DAG status is re-read via `taskflow_summary(flow_id)`.

CRUD interface:

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """Full replacement: DELETE + INSERT (transactional)"""

async def get_todos(session_id: str) -> list[dict]:
    """Read sorted by position"""

def get_todos_sync(session_id: str) -> list[dict]:
    """Sync path for system prompt injection (no event loop)"""

async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """Read todos linked to a TaskFlow flow (map DAG status back to UI)"""
```
