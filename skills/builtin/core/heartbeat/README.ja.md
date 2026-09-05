# Heartbeat — 定期的なタスク確認サービス

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Heartbeat** は EMA AI Agent の定期的なウェイクアップサービスです。各 tick で [`workspace/HEARTBEAT.md`](../../../../workspace/HEARTBEAT.md) を読み込み、補助 LLM がアクティブなタスクの有無を判断します。タスクがあれば専用のエージェント実行で処理し、その結果を通知ゲート経由で配信します。

---

## 動機

会話が終了した後も、エージェントはアイドル状態であっても外部には作業が残っていることがあります:
- 実行待ちのタスク（エージェントやユーザーが HEARTBEAT.md に記述）
- 定期的なチェックを必要とする監視タスク
- 継続的な進行が必要な長時間実行の作業

Heartbeat は、アイドル期間中にエージェントがプロアクティブに作業できるようにする**軽量ポーリングメカニズム**を提供します。

---

## アーキテクチャ

```
┌──────────────────────────────────────────┐
│            HeartbeatService              │
├──────────────────────────────────────────┤
│  asyncio loop (sleep backoff → tick)     │
│  ├─ Phase 1: Read HEARTBEAT.md           │
│  ├─ Phase 2: LLM decision (skip/run)     │
│  └─ Phase 3: Execute + notification gate │
└──────────────────────────────────────────┘
```

### モジュールの責務

| ファイル | 責務 |
|------|---------------|
| [`scripts/base.py`](scripts/base.py) | `HeartbeatService` クラス: asyncio ループ、LLM 決定（`_decide`）、tick パイプライン。モジュールレベルの `heartbeat_service` シングルトン |
| [`scripts/core.py`](scripts/core.py) | HEARTBEAT.md 管理: `ensure_heartbeat_file_exists`、`add_task_to_heartbeat`、`list_active_tasks`、`list_completed_tasks`、`move_task_to_completed`、`remove_tasks_from_completed` / `clear_completed_tasks` |
| [`scripts/evaluate.py`](scripts/evaluate.py) | `evaluate_response()`: 結果を配信する価値があるかを判断する通知ゲート |
| [`server/service/heartbeat.py`](../../../../server/service/heartbeat.py) | 統合層: `process_heartbeat_task`（実行エージェント）、`process_heartbeat_notify`（チャネル配信）、ファイル読み書きヘルパー |
| [`server/trigger/channels/core.py`](../../../../server/trigger/channels/core.py) | `on_execute` / `on_notify` を結び付け、チャネルマネージャーのイベントループ上でサービスを起動 |

---

## HEARTBEAT.md ファイル

- 配置場所は `workspace/HEARTBEAT.md` — [`config/path.py`](../../../../config/path.py) の `HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"`。
- ファイルが存在しない場合、`ensure_heartbeat_file_exists()` が言語非依存のテンプレート `workspace/template/HEARTBEAT.md` をコピーします。
- 骨格フォーマット（`workspace/HEARTBEAT.md` と同じ）:

```markdown
# Heartbeat Tasks

## Active Tasks

## Completed
```

解析ルール（`scripts/core.py` に実装）:
- 各セクションは `## Active Tasks` / `## Completed` の**行全体の完全一致**で特定します（セクションが見つからない場合は `ValueError`）。
- セクションの**コンテンツ行**とは、`<!--`（HTML コメント）で始まらない非空行のことで、次の `##` 見出しまたはファイル末尾までを対象とします。
- タスクは Markdown のリスト項目です。`add_task_to_heartbeat()` は `-` で始まらないテキストに `- [ ] ` プレフィックスを付与します。
- サーバー側の書き込み API には、タスクテキスト上限 `HEARTBEAT_MAX_CONTENT_LENGTH = 2000` 文字があります（見出し・空行・`- ` マーカーはカウントされません。`heartbeat_content_length()` と同じ挙動）。

---

## ワークフロー

```
start() → asyncio task
   └─ loop: sleep(backoff.current_interval) → tick()   # first tick happens after one full interval
        ↓
   Read HEARTBEAT.md (empty/missing → skip tick)
        ↓
   _decide() — auxiliary LLM, virtual tool call:
     ├─ "skip" → log OK, wait for next tick
     └─ "run"  → on_execute(tasks)         # server: one-shot main-LLM agent
                    ↓
              response non-empty → evaluate_response():
                ├─ True  → on_notify(response)   # server: channel delivery
                └─ False → silenced (logged)

tick で例外 → backoff.record_failure()：次の sleep は 2 倍（interval_s × 2ⁿ、
上限 7200 秒）。連続 5 回の失敗でループは終了（CRITICAL ログ）。
tick 成功 → backoff.record_success()：interval_s へ完全リセット。
```

### Phase 1: 読み取り

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

- ファイルが空 → その tick はスキップ（デバッグログ）。
- ファイルが存在しない → `read_text()` が `FileNotFoundError` を送出。ループはエラーを記録して次の周期へ継続します。これはバックオフ失敗としては**記録されません**（ファイル読み取りは tick のバックオフ集計対象 `try/except` の外にあります）。

### Phase 2: 決定（`_decide`）

補助 LLM（`models` の `build_auxiliary_llm()`）は、現在時刻（`current_time_str(self.timezone)`）と HEARTBEAT.md の全内容を受け取り、**仮想ツールコール**で判断を報告します。信頼性の低い自由形式テキストの解析を回避します:

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "description": "Report heartbeat decision after reviewing tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["skip", "run"],
                    "description": "skip = nothing to do, run = has active tasks",
                },
                "tasks": {
                    "type": "string",
                    "description": "Natural-language summary of active tasks (required for run)",
                },
            },
            "required": ["action"],
        },
    },
}]
```

- まず `bind_tools` パスを試行。`tool_calls` が空の場合は `skip` として扱います。
- `NotImplementedError`（ツール非対応のローカル GGUF 系など）やその他の例外が発生した場合は、`with_structured_output(_HeartbeatDecision)`（`action` フィールドが `^(skip|run)$` パターンで制約された Pydantic モデル）へフォールバックします。それも失敗した場合はデフォルトで `("skip", "")` を返します。

### Phase 3: 実行と通知ゲート

`_tick()` の実際のロジック（`scripts/base.py`）:

```python
action, tasks = self._decide(content)
if action != "run":
    return  # "Heartbeat: OK (nothing to report)"

if self.on_execute:
    response: str = await self.on_execute(tasks)
    if response:
        should_notify: bool = evaluate_response(response, tasks)
        if should_notify and self.on_notify:
            await self.on_notify(response)
        # else: "Heartbeat: silenced by post-run evaluation"
```

- `run` → `on_execute(tasks)` がタスクを実行。**空でない**応答のみが `evaluate_response()` で評価され、肯定判定のときだけ `on_notify()` に到達します。
- tick 内で例外が発生すると記録（`logger.exception`）され、**バックオフ失敗**としてカウントされます：次の sleep は 2 倍（interval_s × 2ⁿ、上限 7200 秒）になり、失敗理由が保持されます。tick が成功するとバックオフは完全にリセットされます。
- **連続 5 回**の失敗でループは自ら停止し、CRITICAL ログを出力します（"Heartbeat paused ... manual recovery required"）。スケジュールの再開はプロセス再起動のみです。`trigger_now()` は引き続き単発 tick を実行できます。[`runtime/periodic_backoff.py`](../../../../runtime/periodic_backoff.py) と[暴走ループ防止ハーネス文書](../../../../docs/harness/loop-prevention/README.md)を参照。

---

## HEARTBEAT.md タスク管理 API（`scripts/core.py`）

エージェント向けの関数群。[SKILL.md](SKILL.md) を通じてモデルに公開されます:

| 関数 | 動作 |
|---|---|
| `ensure_heartbeat_file_exists()` | ファイルが存在しない場合、`workspace/template/HEARTBEAT.md` を `workspace/HEARTBEAT.md` としてコピー |
| `add_task_to_heartbeat(task_text, index=None)` | `## Active Tasks` の下にタスクを追加。リスト項目でないテキストには `- [ ] ` プレフィックスを付与。`index` はセクションのコンテンツ行内の 0 始まり挿入位置（範囲外なら `IndexError`）。`None` は末尾に追加 |
| `list_active_tasks()` / `list_completed_tasks()` | `## Active Tasks` / `## Completed` のコンテンツ行を返す |
| `move_task_to_completed(task_text)` | Active Tasks の行を部分一致（前後の空白を除去して比較）。最初に一致した行を削除し、`## Completed` の末尾に追加（セクションが空なら見出しの直後）。一致なし → `ValueError` |
| `remove_tasks_from_completed(task_text=None)` | `None` → コンテンツ行を**すべて**削除。`str` / `list[str]` → 部分一致で削除（1 件も一致しなければ `ValueError`）。処理後にセクション内の連続する空行を圧縮 |
| `clear_completed_tasks(task_text=None)` | `remove_tasks_from_completed` のエイリアス |

これらはすべて `skills.builtin.core.heartbeat.scripts` からエクスポートされます。パッケージ `skills.builtin.core.heartbeat` 自体は `heartbeat_service` シングルトンのみを再エクスポートします。

---

## サーバー統合

サービスはチャネル層 `server/trigger/channels/core.py` が結び付けと起動を担当します:

```python
heartbeat_service.on_execute = _process_heartbeat_task   # → server.service.process_heartbeat_task
heartbeat_service.on_notify = _process_heartbeat_notify  # → server.service.process_heartbeat_notify
asyncio.run_coroutine_threadsafe(heartbeat_service.start(), event_loop)  # channel manager loop
```

**実行（`process_heartbeat_task`）**:
1. `ensure_workspace_system_files()` がコアのペルソナファイルの存在を保証します。
2. 使い捨ての `create_agent(model=build_main_llm(), tools=[python_repl, read_file, write_file])` を構築。システムプロンプトはコアペルソナ（`build_system_prompt(selected_file_names=CORE_SYSTEM_FILE_NAMES)`）、タスク要約を `HumanMessage` として渡します。
3. 最後のメッセージの内容を実行結果とします。
4. 実行済みタスクを Active → Completed へ移動。まず `move_task_to_completed(task)` を試行し、`ValueError`（タスクテキストの不一致）の場合は残りの**すべての**アクティブタスクを移動するフォールバックを実行します。
5. セッション `default` へベストエフォートの WebSocket イベントを 2 つ送信: `heartbeat:updated`（更新後のファイル内容）と `notification`（`heartbeat: ` プレフィックス付きの結果）。失敗はログに記録されるだけで再送出されません。内部例外時は `"Error occurred: {e}"` を返します。

階層に注意: 上記の WebSocket イベントは `process_heartbeat_task` が**成功するたびに**送信するものであり、**チャネル配信**（下記）こそが `evaluate_response()` ゲートの制御対象です。

**配信（`process_heartbeat_notify`）**: `plugins/channels/config.json` を読み込み、設定に `"heartbeat": true` を持ち `receiver` を解決できるチャネル（`plugins/channels/<name>/config.json` 由来。ルートブロックへのフォールバックあり）が、`channel_manager.get_channel(name).send(OutboundMessage(...))` で結果を受け取ります。

**HTTP API**（`server/trigger/http/heartbeat.py`）: `GET /heartbeat` は `{"HEARTBEAT.md": "<content>"}` を返し（ファイルがなければ空 dict）。`PUT /heartbeat` は `{"file_to_content": {"HEARTBEAT.md": "..."}}` を受け付け、2000 文字のタスクテキスト上限を強制します。

---

## 使用例

### 基本的な使用方法（シングルトン）

```python
from skills.builtin.core.heartbeat import heartbeat_service

heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier        # async (response: str) -> None

await heartbeat_service.start()  # デフォルト間隔: 1800 秒（30 分）
```

本番ではこの結び付けは `server/trigger/channels/core.py` にあり、チャネルマネージャーのイベントループ上で実行されます。

### 手動トリガー

```python
result = await heartbeat_service.trigger_now()
```

`trigger_now()` はファイルを読み、`_decide` を実行し、`run` なら `on_execute(tasks)` を待ちます。通知ゲートは**実行されず**、`on_notify` も**呼ばれません**。ファイルが空、判断が `skip`、`on_execute` 未設定の場合は `None` を返します。

### カスタム設定

```python
from skills.builtin.core.heartbeat.scripts.base import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 分
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

（`HeartbeatService` クラスは `scripts/base.py` に定義されています。パッケージの `__init__.py` からは再エクスポートされていません。）

### 停止

```python
heartbeat_service.stop()  # _running = False に設定し、asyncio タスクをキャンセル
```

---

## 設定

| パラメータ | デフォルト | 説明 |
|-----------|---------|-------------|
| `interval_s` | `30 * 60`（1800 秒） | tick 間の秒数。ループは各 tick の**前に sleep する**ため、最初のチェックは `start()` の 1 周期後に発生します。失敗バックオフの基底間隔でもあります |
| 失敗バックオフ | `factor=2.0`、上限 `7200 秒`、`5` 回で停止 | `HeartbeatService.__init__` にハードコードされた `PeriodicBackoff` パラメータ（`runtime/periodic_backoff.py`）。連続する tick 失敗で sleep は最大 2 時間まで伸び、その後は再起動までサービスが停止します |
| `enabled` | `True` | `False` の場合、`start()` は "Heartbeat disabled" をログ出力して何もしません |
| `timezone` | `None` | 決定プロンプトの "Current Time" 行のために `current_time_str()` へ渡されます |
| `on_execute` / `on_notify` | `None` | 非同期コールバック。未設定の場合、実行 / 配信はスキップされます |
| `HEARTBEAT_PATH` | `workspace/HEARTBEAT.md` | `config/path.py` で定義 |
| `HEARTBEAT_TEMPLATE_PATH` | `workspace/template/HEARTBEAT.md` | `ensure_heartbeat_file_exists()` のテンプレートソース |
| `HEARTBEAT_MAX_CONTENT_LENGTH` | `2000` | サーバー書き込み API（`write_heartbeat_file`）が強制するタスクテキスト上限 |

---

## 通知ゲート戦略

`scripts/evaluate.py` の `evaluate_response(response, task_context)` は、仮想 `evaluate_notification` ツール（`should_notify` ブール値・必須、`reason` 文字列）で補助 LLM に判断を求めます。システムプロンプトの内容:

| 通知（`should_notify: true`） | 抑制（`should_notify: false`） |
|--------------------------------|-----------------------------------|
| 実行可能な情報 | 新しい情報のないルーチン状態チェック |
| エラー | すべて正常であることの確認 |
| 完了した成果物 | 実質的に空の応答 |
| ユーザーが明示的にリマインドを依頼した事項 | |

失敗時の挙動: ツールコールが返らない場合や例外発生時は **`True`（通知）** となり、重要なメッセージが静かに破棄されることはありません。`_decide` と異なり、`with_structured_output` のフォールバックはありません。

---

## 混同注意: `HeartbeatStaleness` ミドルウェア

[`agent/middlewares/heartbeat_staleness.py`](../../../../agent/middlewares/heartbeat_staleness.py) は "heartbeat" という名前を共有していますが、**別のサブシステム**です。エージェントのターンがスタックしたことを検出するターンごとのウォッチドッグであり、`before_agent` で `timer_call_register` 経由の 1 分タイマーを開始し、`(heartbeat_iter, heartbeat_tool)` の進捗を追跡します。アイドル状態で 7 サイクル、ツール実行中で 20 サイクル進捗がないとターンを killed としてマークし、次のモデル/ツール呼び出しが `HeartbeatTimeoutError` を送出します。HEARTBEAT.md は読みませんし、本サービスの一部でもありません。

---

## 技術スタック

| コンポーネント | 技術 |
|-----------|-----------|
| ランタイム | Python asyncio（`asyncio.create_task` による単一の `asyncio.Task` ループ） |
| 決定とゲート | 補助 LLM（`models` の `build_auxiliary_llm()`）、LangChain の仮想ツールコール（`bind_tools`）。`_decide` には `with_structured_output` フォールバックあり |
| ファイル I/O | `pathlib` |
| ロギング | `loguru` |
| バリデーション | Pydantic（`_HeartbeatDecision` フォールバックモデル） |
| パス | `config.path`（`HEARTBEAT_PATH`、`HEARTBEAT_TEMPLATE_PATH`） |
