# ⏳ 長時間タスク：TaskFlow、予算、締め切り、記憶、継続性

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが単一ターンを超えて生き続ける作業をどう実行するか：永続化された SQLite DAG エンジン（`taskflow_*`、12 ツール）が、依存関係を持つステップを会話ターンをまたいで追跡し、各ステップを分離された子エージェントへディスパッチし、予算に対してトークン/コスト消費を集計し、バックグラウンド sweeper が期限切れやアイドル状態の flow を失効させ、3 層メモリシステム、圧縮前メモリフラッシュ、要約と TaskFlow の橋渡し、ツール出力の一行要約、セッション間の継続性、そしてアクティブな flow のシステムプロンプトへの自動再注入を通じて、コンテキストを先へ引き継ぎます。

一次情報：`agent/tools/taskflow/**`、`agent/tools/memory.py`、`agent/tools/memory_tiered.py`、`agent/middlewares/memory_flush.py`、`agent/middlewares/summarization.py`（LT-7 ブロック）、`agent/middlewares/task_intent.py`、`agent/middlewares/todo_continuation.py`、`context_engine/session_continuity.py`、`workspace/prompt_builder.py`、`pub/func/message/tool_output_prune.py`、`agent/tools/subagent/registry/sweeper.py`、`config/features/**`。以下の定数、シグネチャ、行番号はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [TaskFlow DAG エンジン](#-taskflow-dag-エンジン)
- [トークン / コスト予算](#-トークン--コスト予算)
- [タスク締め切り](#-タスク締め切り)
- [進捗レポート](#-進捗レポート)
- [アイドル検出](#-アイドル検出)
- [階層メモリ](#-階層メモリ)
- [圧縮前メモリフラッシュ](#-圧縮前メモリフラッシュ)
- [要約 ↔ TaskFlow 連携](#-要約--taskflow-連携)
- [ツール出力の要約](#-ツール出力の要約)
- [セッション継続性](#-セッション継続性)
- [TaskFlow 自動再開](#-taskflow-自動再開)
- [設定レジストリ](#-設定レジストリ)
- [アーキテクチャ図](#-アーキテクチャ図)
- [API リファレンス](#-api-リファレンス)
- [テスト](#-テスト)
- [既知の制限](#-既知の制限)

## 🎯 概要

長時間タスクスタックは、メインエージェントがマルチターンの作業を**永続的な flow** へ分解し、そのステップ同士が依存し合えるようにし、各準備完了ステップを子エージェントへディスパッチし、プロセス再起動を生き延びられるようにします。7 つのサブシステムが協調します：

| # | サブシステム | 入口 | 永続化先 |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG エンジン** | `agent/tools/taskflow/` | `data/taskflow_registry.db`（SQLite、WAL） |
| 2 | **トークン / コスト予算** | `taskflow_budget`、`taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **締め切り** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **アイドル検出** | sweeper `_scan_stale_waiting_taskflows` | `wait_json` の古いマーカー |
| 5 | **階層メモリ** | `memory` ツールアクション + `agent/tools/memory_tiered.py` | `workspace/memory/*.md` + `facts/*.md` |
| 6 | **圧縮前フラッシュ** | `agent/middlewares/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 7 | **継続性 / 自動再開** | `context_engine/session_continuity.py`、`workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + プロンプトブロック |

全体を貫く設計契約は**エラーをテキストとして返す**ことです：ツールは業務エラーをモデルへ投げず、`Error:` で始まる人間可読な文字列を返します。すべてのバックグラウンドフックは**フェイルオープン**です——レジストリが使えなくても sweeper がクラッシュしても、「長時間タスクのコンテキストなし」へ退化するだけで、ターンを壊すことはありません。

## 🧩 TaskFlow DAG エンジン

### ステータス列挙（`agent/tools/taskflow/config.py`）

```python
TABLE_NAME = "task_flows"          # config.py:5
INITIAL_REVISION = 1               # config.py:8

class TaskFlowStatus(StrEnum):     # config.py:11
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class StepStatus(StrEnum):         # config.py:21
    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})   # config.py:35
```

### テーブルスキーマ（`agent/tools/taskflow/registry/store_sqlite.py`）

```sql
CREATE TABLE IF NOT EXISTS task_flows (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL
);
```

DAG 自体（`steps[]`、`results[]`、`depends_on`、`creator_session_key`）は完全に `state_json` の中にあります——DAG フィールドの追加にスキーマ移行は不要です。トークン/コスト/締め切りの列は追加的マイグレーションで導入されました（`_TOKEN_COLUMN_DDL`、`_DEADLINE_COLUMN_DDL`、`store_sqlite.py:83-129`）。WAL プラグマはプロセスごとに一度だけ切り替えられ、すべてのステートメントの前に `PRAGMA busy_timeout = 5000` が実行されます（`store_sqlite.py:234-271`）。

### ステップ状態機械

```
blocked ──(依存満足)──▶ ready ──(ディスパッチ)──▶ dispatched ──(再開)──▶ done
```

| ステータス | 意味 | 設定主体 |
| :--- | :--- | :--- |
| `blocked` | 少なくとも 1 つの `depends_on` が `done` でない；子は生成されない | `taskflow_run_task` |
| `ready` | すべての依存が満たされ、ディスパッチ待ち | 登録時、または再開後の `unlock_dependents` |
| `dispatched` | 分離された子セッションが生成され、`child_session_key` と `dispatched_at` を記録 | `taskflow_run_task` / `taskflow_dispatch` |
| `done` | `taskflow_resume` によって結果が注入された | `taskflow_resume` |

`done` の意味は**「結果が注入された」**であり、「子が成功した」ではありません（[既知の制限](#-既知の制限)を参照）。`status` フィールドを持たない旧ステップは導出されます：`child_session_key` を持つステップは `dispatched`、それ以外は `ready` とみなされます（`_shared.py:85`、`step_status`）。

### `depends_on` の意味論

`deps_satisfied(step, steps)`（`_shared.py:99`）は、すべての `depends_on` id が現在のステップ一覧に存在し**かつ** `done` である場合にのみ真です。`depends_on` が欠落/空なら自明に満たされます。未知の依存 id は決して満たされず、**自己依存も決して満たされません**——そのため自己参照ステップはアンロックループに陥らず安全にブロックされ続けます。`unlock_dependents(steps)`（`_shared.py:137`）はリストを**一度だけ走査**するため、依存サイクルがループすることを構造的に防ぎます。`taskflow_run_task` はディスパッチや状態変更の**前に**未知の依存 id を拒否します。

### ツールファミリ（12 ツール）

すべてのツールは `async` で、`@tool("taskflow_…")` としてデコレートされ、`build_taskflow_tools()`（`tools/__init__.py:43-55`）が `metadata={"scope": "main_only"}` と `handle_tool_error=True` を付与します。共有 flow 状態を管理できるのはメインエージェントだけであり、サブエージェントのツールポリシーはファミリ全体を無条件に除外します。

| ツール | 目的 |
| :--- | :--- |
| `taskflow_create` | リビジョン 1 で flow を作成；任意で `deadline_hours` を設定 |
| `taskflow_run_task` | ステップを登録してディスパッチ（または `blocked` として記録） |
| `taskflow_dispatch` | 複数の準備完了ステップをオール・オア・ナッシングで一括ディスパッチ |
| `taskflow_wait_all` | flow スコープの有界ポーリングでディスパッチ済みステップの確定を待つ |
| `taskflow_resume` | 子の結果を冪等に注入し、後続をアンロックし、トークンを集計 |
| `taskflow_set_waiting` | 理由付きで flow を `waiting` に停める |
| `taskflow_summary` | 読み取り専用の再読込（競合後の再読込にも使う） |
| `taskflow_progress` | 人間可読な進捗/完了レポート |
| `taskflow_budget` | トークン/コスト予算の照会または設定 |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | 終端遷移 |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:38
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

### ディスパッチ——`taskflow_dispatch` の一括意味論

`taskflow_dispatch(flow_id, step_ids, expected_revision=None, session_id="")`（`taskflow_dispatch.py:36`）は、何かを生成する**前に****すべての** id を検証します。ステップが `ready` であるか、`blocked` でも依存が満たされていればディスパッチ可能です。未知の id、重複 id、すでに `dispatched`/`done` のステップは、呼び出し全体を拒否し、生成を一切行いません。成功時、ステップは共有の `_dispatch.dispatch_child` シーム（`_dispatch.py:10`）を通じて順次生成され、**1 回**の `update_flow` 呼び出しで永続化されます。バッチ途中で生成が失敗するとループは停止し、すでに生成済みの子は永続化されるため、子が黙って失われることはありません。エラーは失敗した step id とディスパッチ済み step id の両方を示します。flow レベルの `child_session_key` は意図的に変更しません——各ステップ自身の child key が権威です。

### 待機——`taskflow_wait_all` の flow スコープ

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")`（`taskflow_wait_all.py:110`）は、*この* flow の `dispatched` ステップに記録された子セッション**だけ**をポーリングするため、無関係なバックグラウンドの子が返却をブロックすることは決してありません。未知またはすでにクリーンアップされた run は確定済みとして扱われます（決してハングしません）。ポーリング間隔は `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]`（0.05 秒）にクランプされます。タイムアウト時は部分レポートを返し、確定済みの子に対して `taskflow_resume` を呼び、再度 `wait_all` を呼ぶよう指示します。リクエスタセッションのすべての子が確定したときにのみ発火する `sessions_yield` は、意図的に再利用していません。

### 楽観的ロック

すべての変更は `UPDATE … WHERE flow_id = ? AND expected_revision = ?` を通り、リビジョンを正確に 1 だけ進めます（`store_sqlite.py:460-481`）。一致する行がゼロなら競合です：

```python
# FlowConflictError メッセージ（store_sqlite.py:173）
"TaskFlow '<id>' revision conflict: expected_revision=2 but latest revision=3;
 re-read with taskflow_summary and retry with expected_revision=3"
```

子が**すでに生成済み**なのに書き込みが競合に負けた場合、`update_flow_with_conflict_retry()`（`_shared.py:191`）が最新の flow を読み直し、`build_state` コールバックで状態を再構築し、`PERSIST_MAX_ATTEMPTS = 3` 回まで再試行します。flow が消滅もしくは終端になった場合、または再試行が尽きた場合は、生成済みのすべての `child_session_key` を列挙したエラーを返し、呼び出し側に**key で回収し、決して再ディスパッチしない**よう指示します。

## 🪙 トークン / コスト予算

`taskflow_budget`（`taskflow_budget.py:10`）は照会と設定の両方を担います：

```python
@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",           # "query" | "set"
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

- **`query`** は `total_tokens`、`total_cost`、予算、残りトークン、そしてステータスを報告します：`total_tokens >= budget` なら `EXCEEDED`；使用量が `MODEL_PRICING["budget_warn_threshold"]`（0.80）以上なら `WARNING`；それ以外は `ok`。予算未設定の場合、割合は未設定として報告されます。
- **`set`** は正の整数 `token_budget` を要求し、終端 flow を拒否し、楽観的ロック経由で書き込みます（`expected_revision` を渡すと即座に失敗できます）。

呼び出し側が `token_usage` を渡すと、消費は `taskflow_resume` で集計されます（`taskflow_resume.py:108-122`）：

```python
token_usage={"input_tokens": 1200, "output_tokens": 800, "model_name": "deepseek-chat"}
```

このツールは遅延インポート `from config.features import MODEL_PRICING` を行い、`MODEL_PRICING["model_pricing_per_m_tokens"]` でモデルを検索し（`_default` へフォールバック）、次を計算します：

```
total_tokens = existing_total_tokens + input_tokens + output_tokens
cost_delta   = input_tokens  * pricing["input"]  / 1_000_000
             + output_tokens * pricing["output"] / 1_000_000
total_cost   = round(existing_total_cost + cost_delta, 6)
```

`MODEL_PRICING` は `config/features/infra_side/model_pricing.py` にあります：

| モデル | 入力（USD / 100 万トークン） | 出力（USD / 100 万トークン） |
| :--- | :--- | :--- |
| `glm-5` | 0.5 | 1.5 |
| `deepseek-chat` | 0.14 | 0.28 |
| `kimi-latest` | 0.55 | 2.19 |
| `_default` | 1.0 | 3.0 |

`budget_warn_threshold` = `0.80`。

## ⏰ タスク締め切り

`taskflow_create` は任意の `deadline_hours` を受け取ります（`taskflow_create.py:17`）：

```python
@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
    deadline_hours: float | None = None,
) -> str
```

`deadline_hours` が正の場合、ツールは `deadline_ts = time.time() + deadline_hours * 3600` を `deadline_ts` 列に保存します。`taskflow_summary` は締め切りを描画し、過ぎると `EXCEEDED` とマークします。実際の執行者はバックグラウンド **sweeper**（`agent/tools/subagent/registry/sweeper.py`）で、各スイープサイクルで `_expire_overdue_taskflows()` を実行します（`sweeper.py:123`）：

```python
overdue = await taskflow_store.get_overdue_flows(time.time())
for flow in overdue:
    state["failure_reason"] = "Deadline exceeded: " + time.strftime("%Y-%m-%d %H:%M", ...)
    update_flow(flow_id, flow["expected_revision"], state=state, status=TaskFlowStatus.FAILED.value)
```

期限切れの flow は `failed` とマークされます；すでに終端の flow はクエリから除外され、flow ごとの例外はログに記録されて握りつぶされるため、1 件の不正データがスイープ全体を中断させることはありません。

## 📊 進捗レポート

`taskflow_progress`（`taskflow_progress.py:22`）は、完了率、内訳、次のステップ、残り時間の推定を含む読み取り専用レポートです：

```python
@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str
```

出力形状：

```
Progress Report: <flow_id>
  Status: running
  Description: <先頭 80 文字>
  Completion: 3/5 steps (60%)
  Breakdown: done=3 · dispatched=1 · ready=0 · blocked=1
  Next steps:
    → [step-4] <タスク、先頭 60 文字>
    ⊘ [step-5] <タスク、先頭 60 文字>
  Est. remaining: ~12.5 minutes (based on 3 completed steps)
  Waiting on: <待機理由>               # flow が waiting のときのみ
  Results injected: 3                  # 結果が存在するときのみ
```

ステータスアイコンは `done=✓`、`dispatched=→`、`ready=○`、`blocked=⊘`（`taskflow_progress.py:14`）。推定は、少なくとも**2 つ**の `done` ステップが `dispatched_at` タイムスタンプを持つ場合にのみ生成されます；ディスパッチ時刻間の平均間隔を取り、残りステップ数を掛けます。ステップが無い flow は `Progress: flow_id=…, status=…` と `No steps registered yet.` を返します。

## 💤 アイドル検出

`taskflow_set_waiting` で停めた flow は、子がクラッシュして再開されないままになることがあります。sweeper は `_scan_stale_waiting_taskflows()` でこれを検出します（`sweeper.py:154`）：

1. すべての `waiting` flow を読み込む。
2. `timeout_secs = TASKFLOW_INFRA["waiting_timeout_hours"] * 3600`（既定 24 時間）。
3. `set_at` を持つ待機ペイロードについて、`now - set_at <= timeout_secs` の間はスキップ。
4. `get_run_by_child_session_key(flow["child_session_key"])` + `is_live_unended_run(run)` で子の生存を確認。子がまだ生きていればスキップ；生存確認のインポート自体が失敗した場合は子を死亡とみなす。
5. それ以外の場合は `wait_json` に**非破壊マーカー**を刻む：`stale_detected_at` と `stale_child_session_key`。

アイドル検出が flow を**自動的に失敗させることは決してありません**——マーカーは助言的で、サイクルごとに更新されます。これとは独立に、`taskflow_summary` は待機ステータスを描画し、待機が `TASKFLOW_INFRA["waiting_timeout_hours"]`（24 時間）を超えると `wait_status: STALE (waiting X.Xh, timeout=24h) — child session may have crashed; consider taskflow_resume with a failure result or re-dispatch` を出力します（`taskflow_summary.py:67-90`）。

## 🧠 階層メモリ

3 つの層は、*どのように*モデルへ届くかで区別されます：

| 層 | ストア | 場所 | プロンプトに入るか？ |
| :--- | :--- | :--- | :--- |
| **L1 —— 精錬メモリ** | `MEMORY.md`（エージェントのノート）+ `USER.md`（ユーザープロファイル） | `workspace/memory/`（`MEMORY_DIR`） | はい——凍結スナップショットとして常に注入 |
| **L2 —— 構造化事実** | `facts/{category}.md`、固定 5 カテゴリ | `workspace/memory/facts/`（`FACTS_DIR`） | 一行インデックスのみ；全文はオンデマンドで読む |
| **L3 —— 生の履歴** | `mes_memory.db`（SQLite、WAL、FTS5） | `src/store/mes_memory/mes_memory.db` | いいえ——`context_engine` / `message_search` が取得 |

ファイルは**単独行の区切り文字 `§` で区切られたプレーンテキストのエントリ**です——`ENTRY_DELIMITER = "\n§\n"`（`agent/tools/memory.py:53`）。YAML frontmatter も箇条書きプレフィックスもありません。エントリは複数行にわたれます。

層 1 は `MemoryStore` が管理します（`memory.py:104`）：ファイルごとの文字数上限は `2200`（memory）と `1375`（user）、注入スキャン（`_MEMORY_THREAT_PATTERNS`、`memory.py:68`）がプロンプトインジェクションと認証情報漏洩を拒否し、クロスプラットフォームのファイルロック、アトミック書き込み、完全一致の重複排除を備えます。ライブのエントリは即座に変更される一方、プロンプトは `load_from_disk()` で取得された**凍結スナップショット**を使い、セッション中のプレフィックスキャッシュを安定させます。

層 2 は `TieredMemoryStore` が管理します（`agent/tools/memory_tiered.py:19`）。カテゴリは `environment`、`project`、`decisions`、`user_prefs`、`tool_lessons`（`TIERED_MEMORY["facts_categories"]`）、ファイルごとの上限は `TIERED_MEMORY["facts_char_limit"]` = 4000 文字です。ファイルがあふれると**最も古い**エントリから追い出され、完全な重複は既存として報告されます。

事実操作は独立したツールではなく、**単一の `memory` ツールのアクション**です（`memory.py:641`）：

```python
# MemoryActionSchema.action
Literal["add", "replace", "remove", "fact_add", "fact_read", "fact_search"]
```

| アクション | 必須引数 | 戻り値 |
| :--- | :--- | :--- |
| `fact_add` | `content`、`target`（カテゴリ） | JSON `{"success", "message", "category", "entry_count", "usage"}` |
| `fact_read` | `target` = カテゴリ または `"all"` | JSON `{"success": true, "facts": {category: text}}` |
| `fact_search` | `content`（部分文字列クエリ） | JSON `{"success": true, "results": [{"category", "fact"}], "count"}` |

`prompt_builder.build_system_prompt` は `format_for_system_prompt` 経由で L1 スナップショットを注入し、L2 インデックス `FACTS (on-demand, use memory tool with fact_read/fact_search): …` を追記します（`workspace/prompt_builder.py:271-282`）。`memory` ツールは `scope="main_only"` とタグ付けされているため、サブエージェントには決して見えません。

## 🔥 圧縮前メモリフラッシュ

要約ミドルウェアが古いメッセージを破棄する前に、`agent/middlewares/memory_flush.py` は安価なモデルへ、永続的な事実を `MEMORY.md` に保存する最後の機会を与えます。トリガーは `should_flush(discarded_messages, estimated_tokens)`（`memory_flush.py:43`）：

```python
if not MEMORY_FLUSH["enabled"]:
    return False
total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
if total_chars >= MEMORY_FLUSH["force_flush_chars"]:   # 50_000
    return True
return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]   # 8_000
```

発火すると、`run_memory_flush`（非同期）/ `run_memory_flush_sync` が注入されたファクトリでモデルを構築し、単一のプレーンテキスト抽出プロンプト（`_FLUSH_PROMPT`、`memory_flush.py:19`）を使います。出力は `§` で区切られた `Environment / Project / Decision / User / Tool` の事実リストです。空の結果やリテラル `(none)` はスキップされます。抽出テキストは `MemoryStore.append_entries(new_entries)`（`memory.py:281`）へ渡され、`§` で分割し、各候補を注入スキャンし、既存集合と重複排除し、追記し、2200 文字を超える間は最古のエントリを追い出し、最後に一度のアトミック書き込みを行います。`append_entries` は常に `MEMORY.md` を対象にします。すべての失敗経路は `False` を返して握りつぶされます——フラッシュが圧縮をブロックすることは決してありません。

⚠️ **配線状況。** `Summarization.__init__` は `memory_store` / `llm_factory` を受け取り（どちらも既定 `None`、`summarization.py:623-624`）、両方が設定されている場合にのみ、`_apply_compression`（`summarization.py:1703`）と `_aapply_compression`（`summarization.py:1791`）の中でフラッシュを呼びます。現在の本番インスタンス——メインエージェント `agent/core.py:170` とサブエージェント `agent/tools/subagent/spawn/core.py:784`——はこれらを渡して**いません**。したがってフラッシュは実装・テスト済みですが、呼び出し箇所がストアと `factory(model=…, max_tokens=…, timeout=…)` の形のファクトリを提供するまで潜在状態にあります。

## 🔗 要約 ↔ TaskFlow 連携

圧縮が LLM プロンプトを組み立てるとき、`_get_taskflow_context_sync(session_id)`（`agent/middlewares/summarization.py:262`）がこのセッションのアクティブな flow を描画し、要約プロンプトの**最後**の部分として追記します（`_build_summary_prompt`、`summarization.py:1431-1434`）：

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

このブロックは `## Current TaskFlow State (authoritative)` を見出しとし（`summarization.py:286`）、セッションが所有する最大 3 つの flow（`requester_session_key(session_id)` で照合）について、flow id/ステータス、説明、`done/total` 進捗とステータス内訳、最後の 2 つの完了ステップ、最初の 2 つの保留ステップ、待機理由を列挙します。DAG ヘルパー `step_status` と `steps_summary` を再利用し、完全にフェイルオープンです（`except Exception → ""`）。決定論的フォールバック要約（`_build_static_fallback_summary`）はこのブロックを**含みません**；これは LLM プロンプト専用の追加です。

## ✂️ ツール出力の要約

非 LLM 剪定では、大きすぎる古い `ToolMessage` の内容は通常マーカーへクリアされます。`pub/func/message/tool_output_prune.py` は裸のマーカー `_PRUNE_MARKER = "[Old tool result content cleared]"`（`tool_output_prune.py:22`）を**一行のツール固有要約**へ置き換え、結果に何が含まれていたかの手がかりをモデルに残します：

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_summary"   -> "[taskflow_summary] {len} chars"
"taskflow_run_task"  -> "[taskflow_run_task] dispatched, {len} chars"
"taskflow_resume"    -> "[taskflow_resume] injected result, {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`（`tool_output_prune.py:103`）は新しい順から古い順へメッセージを走査し、最初の要約メッセージで停止し、最新の `prune_protect_tokens`（40 000）を保護し、保護対象ツール（`{"memory", "skill_view", "skill_list"}`）をスキップし、解放トークンが `prune_min_reduction_tokens`（5 000）に達した場合にのみ確定します。置換されたメッセージは `additional_kwargs["status"] = "compacted"` と `["original_length"]` を持つ `model_copy` クローンです。要約は 200 文字に制限され、テンプレート例外はマーカーへフォールバックします。呼び出し元は `Summarization._run_non_llm_strategies` です（`summarization.py:1538`）。

## 🔄 セッション継続性

セッションがクリアされるとき、`context_engine/session_continuity.py` が終了状態を永続化し、次のセッションが継続性を提示できるようにします。`server/DAO/messages.py::clear_session` は削除の前に `auto_save_on_session_end(session_id)` を**ステップ 0** として呼びます（`server/DAO/messages.py:27-33`）。この関数は：

1. `runtime.relation_register` 経由で `channel_id`/`chat_id` を解決します（`_get_channel_chat_for_session`、`session_continuity.py:167`）。
2. 直近 3 ターンを読み、最後の AI 返信を `_MAX_SUMMARY_CHARS = 500` に切り詰めます（`session_continuity.py:28`）。
3. そのセッションのアクティブ flow id を収集します。
4. `save_session_end_state(...)` を `src/data/session_continuity/{safe-key}.json` に書き込みます（`session_continuity.py:25`）。フィールドは `last_session_id`、`ended_at`、`ended_ts`、`summary`、`taskflow_ids` です。

次のセッションは `build_continuity_prompt(session_id)`（`session_continuity.py:80`）でこれを読みます。これは `workspace/prompt_builder.py:169` の `_build_continuity_block` から呼ばれ、完全なプロンプトを構築するときに注入されます（`prompt_builder.py:289-295`）：

```
## Last Session (continuity)
Last conversation ended with: <要約 ≤ 500 文字>
Related tasks: <最大 3 つの flow id>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

セッションが自分自身の状態を受け取ることはありません（`last_session_id == session_id → ""`）。検索には **channel id と chat id の両方**が必要なため、チャネルバインディングのない純粋な WebSocket セッションには継続性ブロックがありません。ストレージはデータベースではなく、ファイルシステム上の JSON（`channel:chat` をキーとし、退化時は `session_id`）です。

## ♻️ TaskFlow 自動再開

アクティブな flow はシステムプロンプトへ再浮上し、新しいセッションが未完了の作業を引き継げるようにします。3 つの独立した読み取りが同じレシピを使います——`requester_session_key(session_id)` + `get_active_flows_sync()` + `state["creator_session_key"]` フィルタ：

| 読み取り | 場所 | 目的 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | システムプロンプトの `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization.py:262` | 圧縮要約プロンプトの TaskFlow ブロック（LT-7） |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:186` | 永続化された継続性状態の `taskflow_ids` |

`creator_session_key` は flow 作成時に `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"` として刻まれます（`taskflow_create.py:38`、`_shared.py:21`）。`get_active_flows_sync()`（`store_sqlite.py:538`）は `running` と `waiting` の flow だけをリビジョン順で返し、イベントループを必要としない stdlib `sqlite3` パスを使います；失敗時は `[]` を返します。

システムプロンプトブロック（`prompt_builder.py:140`）は次の形です：

```
## Pending TaskFlows
- [running] flow-1: "<説明>" | 2/5 steps done | next: step-3 "<タスク>"
Use taskflow_summary to inspect a flow and continue execution.
```

最大 3 つの flow に制限され、ファイルフィルタ付きでプロンプトを構築するとき（`selected_file_names is not None`）は抑制されます。すべての読み取りはフェイルオープンです。

## ⚙️ 設定レジストリ

すべての調整値は `config/features/` 配下に、手作りの**オブジェクト単位 `TypedDict` レジストリ**として存在します。各モジュールは `class XxxConfig(TypedDict)` とモジュールレベルの定数 `XXX: XxxConfig = {…}` を定義します。環境対応モジュールはビルダー `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig` を定義し、`env or os.environ` を読んでインポート時に定数を具体化します。唯一の環境ヘルパーは `_env_int(name, default, env)`（`config/features/_env.py:9`）で、`1/true/yes/on` と `0/false/no/off/""` を受け付け、決して例外を投げません。

レジストリは現在 **38 個の feature オブジェクト**を保持します——エージェント側 20 個（`config/features/agent_side/`）とインフラ側 18 個（`config/features/infra_side/`）——各パッケージの `__init__.py` を通じて再エクスポートされ、`config/features/__init__.py` が集約します。消費側コードは定数をインポートして直接インデックスします（例：`ITERATION_BUDGET["default_max_iterations"]`）。`get_feature`/`load_feature` アクセサは存在しません。`config/__init__.py:38-39` は `GATEWAY` から `API_HOST`/`API_PORT` を導出します。

本文書に最も関係する定数：

| 設定オブジェクト | フィールド | 値 |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA`（`agent_side/taskflow_infra.py`） | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING`（`infra_side/model_pricing.py`） | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `TIERED_MEMORY`（`agent_side/tiered_memory.py`） | `facts_char_limit` | 4000 |
| | `facts_index_max_chars` | 200（宣言済み；ライブコード未消費） |
| | `facts_categories` | environment, project, decisions, user_prefs, tool_lessons |
| `MEMORY_FLUSH`（`agent_side/memory_flush.py`） | `enabled` | `MEMORY_FLUSH_ENABLED`（既定 1） |
| | `model` | `MEMORY_FLUSH_MODEL`（既定 ""） |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION`（`agent_side/summarization.py`） | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY`（`infra_side/mes_memory.py`） | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ アーキテクチャ図

```
                          ┌──────────────────────────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (12, main_only)   │          │  add/fact_add/…      │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │ TieredMemoryStore(L2)│                 │ ─ FACTS index (L2)     │
│ state_json DAG    │          │ mes_memory.db (L3)   │                 │ ─ Pending TaskFlows    │
└────────┬──────────┘          └──────────────────────┘                 │ ─ Last Session         │
         │                                                              └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
                                                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+LT-7 TaskFlow)  │   │
└──────────────────────────────┘                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

## 📚 API リファレンス

### TaskFlow ツール

| ツール | シグネチャ | 戻り値 |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | 作成した id/ステータス/リビジョン（+ 締め切り） |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, session_id)` | ディスパッチ済みステップ、または未充足依存付き `blocked` |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | ディスパッチ済み step id + リビジョン |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | ステップごとの確定レポート（完全または部分） |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None)` | 再開後ステータス、アンロック済みステップ、ステップ状態カウント |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None)` | waiting ステータス + リビジョン |
| `taskflow_summary` | `(flow_id)` | 待機/締め切り状態を含む完全な flow 状態 |
| `taskflow_progress` | `(flow_id)` | 完了率、内訳、次のステップ、残り推定 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None)` | 予算レポート、または設定確認 |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None)` | 終端 `done` |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None)` | 終端 `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None)` | 終端 `cancelled` |

### memory ツールのアクション

| アクション | シグネチャ | 戻り値 |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON 成功/エラー |
| `fact_add` | `memory(action="fact_add", target=<カテゴリ>, content=<事実>)` | JSON `{success, message, category, entry_count, usage}` |
| `fact_read` | `memory(action="fact_read", target=<カテゴリ>\|"all")` | JSON `{success, facts: {category: text}}` |
| `fact_search` | `memory(action="fact_search", content=<クエリ>)` | JSON `{success, results: [{category, fact}], count}` |

### 主要な関数と定数

| シンボル | 場所 | 役割 |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | ライフサイクル / DAG 列挙 |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py:402` | 楽観的ロック付き変更 |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py:538` | セッション横断のアクティブ flow 読み取り |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py:489,505` | sweeper クエリ |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:99,137` | DAG 遷移 |
| `update_flow_with_conflict_retry` | `_shared.py:191` | 生成済みの子を失わない永続化 |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | 締め切りの執行 |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | アイドル検出マーカー |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization.py:262` | LT-7 要約連携 |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | 自動再開プロンプトブロック |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:103` | ツール出力の一行要約 |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | 継続性保存フック |
| `should_flush` / `run_memory_flush` | `agent/middlewares/memory_flush.py:43,65` | 圧縮前フラッシュ |
| `append_entries` | `agent/tools/memory.py:281` | MEMORY.md への一括追記 |

## 🧪 テスト

TaskFlow スイートは `tests/agent/tools/taskflow/` にあります（14 個の `unit` テストファイル + 共有 `conftest.py`）：

| テストファイル | カバー内容 |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD、リビジョン増分、楽観的並行性競合、WAL、同期アクセサ、active/waiting/terminal フィルタ |
| `test_step_graph.py` | `deps_satisfied`、`mark_step_done`、`unlock_dependents`、旧ステータス導出、自己依存ガード |
| `test_summary_dag.py` | `taskflow_summary` の DAG 描画 |
| `test_resume_dag.py` | 再開で done 化、後続アンロック、部分完了、冪等な空操作 |
| `test_taskflow_tools.py` | 再起動を跨ぐ create→run→resume→finish、競合、終端遷移 |
| `test_taskflow_dispatch.py` | 一括ディスパッチ、オール・オア・ナッシング検証、バッチ途中失敗の永続化 |
| `test_dag_e2e.py` | プロセス再起動を跨ぐ完全並列 DAG flow |
| `test_conflict_persistence.py` | 競合後の生成済み子の永続化、再試行枯渇 |
| `test_taskflow_wait_all.py` | flow スコープ待機、タイムアウト部分レポート、無関係な生存子 |
| `test_run_task_dag.py` | ブロック登録、充足後ディスパッチ、未知依存エラー |
| `test_taskflow_progress.py` | 完了率/内訳/次のステップ/残り推定/waiting |
| `test_token_budget.py` | トークン集計、コスト計算、予算照会/設定/警告/超過 |
| `test_deadline.py` | `deadline_hours`、要約描画、sweeper 失効 |
| `test_idle_detection.py` | active/stale 待機状態、sweeper マーカー、生存子スキップ |

横断スイート：`tests/agent/middlewares/test_memory_flush.py`（フラッシュ閾値と `append_entries`）、`tests/agent/tools/test_memory_tiered.py`（階層事実）、`tests/context_engine/test_session_continuity.py`（継続性の保存/プロンプト）、`tests/agent/middlewares/test_todo_continuation.py`（ターン終了時の継続）、`tests/pub/func/message/test_tool_output_prune.py`（一行要約）、`tests/workspace/test_prompt_builder_taskflow.py`（保留 flow のプロンプト注入）。

標準の uv/pytest ツールでこの領域だけを実行：

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py tests/agent/tools/test_memory_tiered.py -q
```

完全なプロセス分離スイートには `uv run python tests/run_tests_split.py` を使います（Group A が `unit` ファイル、Group B が `module`/`integration` ファイルを実行）。

## ⚠️ 既知の制限

- **`done` は成功ではない。** ステップの `done` は「結果が注入された」ことだけを意味し、`failed`/`skipped` のステップ状態は存在しません。子がエラーを報告しても `taskflow_resume` はステップを `done` にして後続をアンロックします。失敗を認識するステップ遷移は意図的に先送りされています。
- **`taskflow_wait_all` は設計上 flow スコープ。** 指定 flow のディスパッチ済みステップに記録された子だけを待ち、未知/クリーンアップ済みの run は確定とみなします。「すべてのアクティブ flow を待つ」グローバルプリミティブはありません。
- **アイドル検出は助言的。** sweeper は `stale_detected_at` / `stale_child_session_key` を `wait_json` に刻みますが、古い `waiting` flow を自動で失敗させることはありません。人間かモデルがマーカーに基づいて行動する必要があります。
- **圧縮前メモリフラッシュは潜在状態。** `Summarization` の本番インスタンス（メイン/サブ）は `memory_store` / `llm_factory` を渡さないため、呼び出し箇所が配線するまでフラッシュは実行されません。コードは実装・テスト済みですが現在は不活性です。
- **継続性はチャネル依存。** `build_continuity_prompt` は channel id と chat id の両方を必要とするため、チャネルバインディングのないセッションは継続性ブロックを受け取りません。ストレージはディスク上のキー別 JSON であり、データベースではありません。
- **アクティブ flow スキャンが 3 重複。** `prompt_builder._build_taskflow_block`、`summarization._get_taskflow_context_sync`、`session_continuity._get_active_taskflow_ids_sync` が同じクエリを独立実装しています；同期を保つ必要があります。
- **レジストリ規模は 35 ではなく 38。** 設定レジストリは 38 個の feature オブジェクト（エージェント側 20 + インフラ側 18）を保持します；インフラ側の契約テストは `MODEL_PRICING` を省くため 17 と過少カウントします。
- **`facts_index_max_chars` は宣言済みだが未使用。** `TIERED_MEMORY` フィールドは存在しますが、ライブコードは読みません。
- **パッケージ再エクスポートの欠落。** `agent/tools/taskflow/__init__.py` は 11 個の名前しか再エクスポートしません；`taskflow_dispatch` と `taskflow_wait_all` は `build_taskflow_tools()` 経由で到達できますが、パッケージ `__all__` から漏れています。
- **LT-7 の TaskFlow ブロックは LLM プロンプト専用。** LLM 失敗時に使われる決定論的フォールバック要約は `## Current TaskFlow State` を含みません。
- **トークン会計は呼び出し側提供。** コストは `taskflow_resume` が `token_usage` 辞書を受け取ったときだけ計算されます；無しで注入されたステップはゼロトークン・ゼロコストに貢献します。
