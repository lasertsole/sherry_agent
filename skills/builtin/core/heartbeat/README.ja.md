# Heartbeat — 定期的なタスク確認サービス

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Heartbeat** は EMA AI Agent の定期的なウェイクアップサービスで、定期的に `HEARTBEAT.md` をチェックして保留中のタスクを確認し、自動的に実行・通知します。

---

## 動機

会話が終了した後も、エージェントは外部に残っている作業がある間アイドル状態でいる可能性があります:
- 結果を待っているバックグラウンドタスク（非同期ツール呼び出し）
- 定期的なチェックを必要とする監視タスク
- 継続的な進行が必要な長時間実行の作業

Heartbeat は、アイドル期間中にエージェントがプロアクティブに作業できるようにする**軽量ポーリングメカニズム**を提供します。

---

## アーキテクチャ

```
┌─────────────────────────────────────┐
│          HeartbeatService            │
├─────────────────────────────────────┤
│  Loop (tick every N seconds)         │
│  ├─ Phase 1: Read HEARTBEAT.md       │
│  ├─ Phase 2: LLM decide (skip/run)   │
│  └─ Phase 3: Execute + notification gate │
└─────────────────────────────────────┘
```

### モジュールの責務

| ファイル | 責務 |
|------|---------------|
| `core.py` | メインサービス: ループ、LLM 決定、タスク実行トリガー |
| `evaluate.py` | 通知ゲート: 結果が配信する価値があるかを判断 |

---

## ワークフロー

```
Timer fires (default 30 min)
     ↓
Read HEARTBEAT.md
     ↓
LLM (tool-call) decision:
  ├─ "skip" → no tasks, wait for next tick
  └─ "run" → execute via on_execute callback
                   ↓
              evaluate_response():
                ├─ True  → on_notify pushes result to user
                └─ False → silent (routine check, nothing new)
```

### Phase 1: 読み取り

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

`HEARTBEAT_PATH` は `config.py` で設定され、プロジェクトの `HEARTBEAT.md` を指します。ファイルが存在しないか空の場合は、その tick はスキップされます。

### Phase 2: 決定

**仮想ツールコール**を使用して LLM にアクティブなタスクがあるかを判断させ、信頼性の低い自由形式テキストの解析を回避します:

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "parameters": {
            "action": {"enum": ["skip", "run"]},
            "tasks": {"type": "string"},  # task summary when run
        },
        "required": ["action"],
    },
}]
```

`skip` → 操作なし; `run` → Phase 3 へ進む。

### Phase 3: 実行と通知ゲート

```python
if action == "run" and self.on_execute:
    response = await self.on_execute(tasks)           # execute task
    should_notify = evaluate_response(response, tasks) # evaluate notification
    if should_notify and self.on_notify:
        await self.on_notify(response)                 # push to user
```

`evaluate_response()` は独立した LLM ツールコールを使用して、応答に**実行可能な情報**（エラー、成果物、ユーザーが要求した結果）が含まれているかを判断し、ルーチン的なステータス更新を抑制します。

---

## 使用例

### 基本的な使用方法

```python
from skills.builtin.core.heartbeat import heartbeat_service

# コールバックを設定
heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier  # async (response: str) -> None

# 開始（デフォルト 30 分間隔）
await heartbeat_service.start()
```

### 手動トリガー

```python
result = await heartbeat_service.trigger_now()
if result:
    print(f"Task result: {result}")
```

### カスタム設定

```python
from skills.builtin.core.heartbeat import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 minutes
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

### 停止

```python
heartbeat_service.stop()
```

---

## 設定

| パラメータ | デフォルト | 説明 |
|-----------|---------|-------------|
| `interval_s` | 1800 (30 min) | tick 間の間隔 |
| `enabled` | True | Heartbeat サービスを有効にする |
| `timezone` | None | LLM 決定用のタイムゾーン（例: "Asia/Shanghai"） |
| `HEARTBEAT_PATH` | config.py 参照 | HEARTBEAT.md へのパス |

---

## 通知ゲート戦略

`evaluate_response()` の決定ロジック:

| 通知 | 抑制 |
|--------|----------|
| エラーまたは例外 | ルーチン確認、異常なし |
| タスク成果物の完了 | すべて正常であることの確認 |
| ユーザーが明示的に要求した情報 | 応答が空または無関係 |

失敗時はデフォルトで `True`(通知) になり、重要なメッセージが静かに破棄されないようにします。

---

## 技術スタック

| コンポーネント | 技術 |
|-----------|-----------|
| ランタイム | Python asyncio |
| LLM 決定 | `auxiliary_llm` (bind_tools) |
| ファイル I/O | pathlib |
| 設定 | `config.HEARTBEAT_PATH` |
