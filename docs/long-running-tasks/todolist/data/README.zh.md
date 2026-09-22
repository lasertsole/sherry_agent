# 📦 TodoList 数据层 — 计划、Boulder、账本与 todos.db

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [TodoList](../README.zh.md) 的一部分：规划层背后的会话级数据存储 —— 计划文件、Boulder 状态、证据账本与 todos.db 结构。

---

## 数据层

### 计划文件 (workspace/sessions/<session_id>/plans/*.md)

计划文件是会话作用域的：**清除会话会删除该会话的 plans**（整个 `workspace/sessions/<session_id>/` 树被删除）。计划引用经 `config.path.resolve_plan_path` 解析到会话树或显式的仓库相对路径；外部编排目录不参与解析。

Checkbox 格式的 Markdown，定义完整的 HTN 分解：

```markdown
# <计划名>

## Goal

<详细目标：计划名、路径、具体终态、交付模式、验证方式>

## Context

<项目背景、约束、已知信息>

## TODOs

### Wave 0: <Wave 描述>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>

### Wave 1: <Wave 描述> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder 状态 (src/data/boulder.json)

持久化工作状态，`session_id` 前缀用 `sherry:`：

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

### 证据账本 (src/data/evidence-ledger.jsonl)

每行一个 JSON 对象，记录每个 checkbox 的执行证据：

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "adversarial_classes": {"stale_state": "not-applicable", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
{"event": "stale", "file_path": "agent/tools/taskflow/step_judge.py", "session_id": "sherry:xxx", "timestamp": "..."}
```

- **自动记录**：`agent/tools/todolist/evidence_recorder.py` 为 `terminal` / `python_repl` 结果中识别出的验证命令追加一行（`kind` + `status`，受 `EVIDENCE_LEDGER["auto_record"]` 控制），并在 `write_file` / `patch_file` 编辑后追加 `stale` 事件（`auto_stale`）。所有入口都失败开放——账本写入绝不破坏工具。
- **陈旧性为推导值**：当且仅当更晚的 `{"event": "stale"}` 行命名了某条证据行 `command` 中包含的路径时，该行才算陈旧（`agent/tools/taskflow/evidence_collector.py`），历史行永不被改写。
- **会话视图**：`EvidenceLedger.for_session(session_key)` / `read_for_session()` 按 `session_id` 过滤共享文件；文件本身保持仓库级共享。

### todos.db — 会话级 TODO 存储

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

| 字段       | 说明                                                              |
| ---------- | ----------------------------------------------------------------- |
| `plan_ref` | 关联的计划文件路径 —— 会话作用域 `workspace/sessions/<session_id>/plans/*.md` |
| `flow_id`  | 关联的 TaskFlow flow id（DAG 由 TaskFlow 持有）                    |
| `step_id`  | 关联的 TaskFlow step id（如 `step-2`），用于回读 DAG 状态          |

> **设计边界**：`todos.db` **不持有** `depends_on` / 波次 / step 状态。依赖图、`blocked/ready/dispatched/done` 与解锁逻辑全部由 TaskFlow 提供。todo 只通过 `flow_id`/`step_id` 指向对应 flow step，DAG 状态用 `taskflow_summary(flow_id)` 回读。

CRUD 接口：

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """全量替换：DELETE + INSERT（事务）"""

async def get_todos(session_id: str) -> list[dict]:
    """按 position 排序读取"""

def get_todos_sync(session_id: str) -> list[dict]:
    """系统提示词注入的同步路径（无事件循环）"""

async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """读取关联到某个 TaskFlow flow 的 todo（把 DAG 状态映射回 UI）"""
```
