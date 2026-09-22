# 📦 TodoList データレイヤー — プラン、Boulder、台帳、todos.db

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [TodoList](../README.ja.md) の一部：計画レイヤーを支えるセッションスコープのデータストア — プランファイル、Boulder 状態、エビデンス台帳、todos.db スキーマ。

---

## データレイヤー

### 計画ファイル (workspace/sessions/<session_id>/plans/*.md)

計画ファイルはセッションスコープです：**セッションを削除するとそのセッションの plans も削除されます**（`workspace/sessions/<session_id>/` ツリー全体が削除）。計画参照は `config.path.resolve_plan_path` によってセッションツリーまたは明示的なリポジトリ相対パスへ解決され、外部オーケストレーションディレクトリは関与しません。

チェックボックス形式の Markdown で、完全な HTN 分解を定義します：

```markdown
# <Plan Name>

## Goal

<詳細な目標：計画名、パス、終端状態、デリバリーモード、検証方法>

## Context

<プロジェクト背景、制約、既知の情報>

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

### Boulder 状態 (src/data/boulder.json)

永続的なワーク状態。`session_id` に `sherry:` プレフィックスを使用：

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": "workspace/sessions/<session_id>/plans/<plan-name>.md",
      "session_ids": ["sherry:<session_id>"],
      "status": "active"
    }
  }
}
```

### エビデンス台帳 (src/data/evidence-ledger.jsonl)

1行1 JSON オブジェクトで、各チェックボックスの実行エビデンスを記録します。

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "artifact": "src/data/evidence/xxx.txt", "adversarial_classes": {"stale_state": "not-applicable", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
{"event": "stale", "file_path": "agent/tools/taskflow/step_judge.py", "session_id": "sherry:xxx", "timestamp": "..."}
```

- **自動記録**：`agent/tools/todolist/evidence_recorder.py` が `terminal` / `python_repl` の結果から認識した検証コマンドの行（`kind` + `status`、分類表は `EVIDENCE_LEDGER["verify_commands"]`）を常時追記し、`write_file` / `patch_file` の編集後に `stale` イベントを追記します。すべての入口はフェイルオープン — 台帳書き込みがツールを壊すことはありません。
- **staleness は導出値**：後続の `{"event": "stale"}` 行が、ある evidence 行の `command` に含まれるパスを名指しした場合にのみ、その行は stale です（`agent/tools/taskflow/evidence_collector.py`）。履歴行は決して書き換えられません。
- **セッションビュー**：`EvidenceLedger.for_session(session_key)` / `read_for_session()` が共有ファイルを `session_id` でフィルタします；ファイル自体はリポジトリ全体で共有されたままです。

### todos.db — セッションレベル TODO ストレージ

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

| フィールド | 説明                                                              |
| ---------- | ----------------------------------------------------------------- |
| `plan_ref` | リンクされた計画ファイルパス — セッションスコープ `workspace/sessions/<session_id>/plans/*.md` |
| `flow_id`  | リンクされた TaskFlow flow id（DAG は TaskFlow が所有）            |
| `step_id`  | リンクされた TaskFlow step id（例：`step-2`）、DAG ステータス再読取用 |

> **設計境界**：`todos.db` は `depends_on` / ウェーブ / step ステータスを保持しません。依存グラフ、`blocked/ready/dispatched/done` とアンロックロジックはすべて TaskFlow が提供します。todo は `flow_id`/`step_id` で対応する flow step を指すだけで、DAG ステータスは `taskflow_summary(flow_id)` で再読取します。

CRUD インターフェース：

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """全量置換：DELETE + INSERT（トランザクション）"""

async def get_todos(session_id: str) -> list[dict]:
    """position 順に読取"""

def get_todos_sync(session_id: str) -> list[dict]:
    """システムプロンプト注入用の同期パス（イベントループなし）"""

async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """TaskFlow flow に関連する todo を読取（DAG ステータスを UI にマッピング）"""
```
