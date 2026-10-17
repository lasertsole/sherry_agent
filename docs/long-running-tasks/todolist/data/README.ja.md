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

1 行 1 JSON オブジェクト：共有の追記専用検証監査トレイルです。書き込み側は `agent/tools/todolist/evidence_recorder.py`、読み取り側は `agent/tools/taskflow/evidence_collector.py` で、書き込み側・行スキーマ・導出される古さのルール・要約形式・これを消費するゲートは[エビデンス台帳](../../gates/evidence-ledger/README.ja.md)にまとめています。

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
