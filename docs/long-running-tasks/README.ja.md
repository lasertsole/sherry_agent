# ⏳ 長時間タスク：TaskFlow、予算、締め切り、記憶、継続性

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが単一ターンを超えて生き続ける作業をどう実行するか：永続化された SQLite DAG エンジン（`taskflow_*`、13 ツール）が、依存関係を持つステップを会話ターンをまたいで追跡し、各ステップを分離された子エージェントへディスパッチし、オプトインのポリシーに従って失敗/死亡ステップを再ディスパッチし、ステップの受け入れ基準をオーケストレータが検証できるようエコーし、予算に対してトークン/コスト消費を集計し、バックグラウンド sweeper が期限切れやアイドル状態の flow を失効させ、グローバルなセッション横断 flow ボードを公開し、3 層メモリシステム、圧縮前メモリフラッシュ、要約と TaskFlow の橋渡し、ツール出力の一行要約、セッション間の継続性、サブエージェント完了時のメモリ還流、そしてアクティブな flow のシステムプロンプトへの自動再注入を通じて、コンテキストを先へ引き継ぎます。

一次情報：`agent/tools/taskflow/**`、`agent/tools/memory.py`、`agent/tools/memory_tiered.py`、`agent/middlewares/memory_flush.py`、`agent/middlewares/summarization.py`（LT-3 事実ベースライン + LT-7 ブロック）、`agent/middlewares/subagent_completion_drain.py`（LT-5 還流）、`agent/middlewares/task_intent.py`、`agent/middlewares/todo_continuation.py`、`context_engine/session_continuity.py`、`workspace/prompt_builder.py`、`pub/func/message/tool_output_prune.py`、`agent/tools/subagent/registry/sweeper.py`、`agent/wrapper/**`、`config/features/**`。以下の定数、シグネチャ、行番号はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [TaskFlow DAG エンジン](#-taskflow-dag-エンジン)
- [ステップ再試行ポリシー（GAP-8）](#-ステップ再試行ポリシーgap-8)
- [トークン / コスト予算](#-トークン--コスト予算)
- [タスク締め切り](#-タスク締め切り)
- [結果検証（GAP-7）](#-結果検証gap-7)
- [進捗レポート](#-進捗レポート)
- [アイドル検出](#-アイドル検出)
- [セッション横断ボード（GAP-9）](#-セッション横断ボードgap-9)
- [階層メモリ](#-階層メモリ)
- [圧縮前メモリフラッシュ](#-圧縮前メモリフラッシュ)
- [要約 ↔ TaskFlow 連携](#-要約--taskflow-連携)
- [サブエージェントメモリ還流（LT-5）](#-サブエージェントメモリ還流lt-5)
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

### ツールファミリ（13 ツール）

すべてのツールは `async` で、`@tool("taskflow_…")` としてデコレートされ、`build_taskflow_tools()`（`tools/__init__.py:46-58`）が `metadata={"scope": "main_only"}` と `handle_tool_error=True` を付与します。共有 flow 状態を管理できるのはメインエージェントだけであり、サブエージェントのツールポリシーはファミリ全体を無条件に除外します。

| ツール | 目的 |
| :--- | :--- |
| `taskflow_create` | リビジョン 1 で flow を作成；任意で `deadline_hours` を設定 |
| `taskflow_run_task` | ステップを登録（任意で `validation_criteria` / `retry_policy`）してディスパッチ（または `blocked` として記録） |
| `taskflow_dispatch` | 複数の準備完了ステップをオール・オア・ナッシングで一括ディスパッチ |
| `taskflow_wait_all` | flow スコープの有界ポーリングでディスパッチ済みステップの確定を待つ（ポリシー付き確定ステップを自動再試行） |
| `taskflow_resume` | 子の結果を冪等に注入し、後続をアンロックし、トークンを集計（失敗認識の再試行 + 基準エコー） |
| `taskflow_set_waiting` | 理由付きで flow を `waiting` に停める |
| `taskflow_summary` | 読み取り専用の再読込（競合後の再読込にも使う） |
| `taskflow_progress` | 人間可読な進捗/完了レポート |
| `taskflow_budget` | トークン/コスト予算の照会または設定 |
| `taskflow_list` | すべての flow のセッション横断ボード（`active` / `all` / ステータス名） |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | 終端遷移 |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:40
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    session_id: SessionId = "",
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

## 🔁 ステップ再試行ポリシー（GAP-8）

ステップは宣言的な `retry_policy` を持てるため、失敗した子はそのステップの最終結果として受け入れられる代わりに自動で再ディスパッチされます。ポリシー、カウンタ、置換子セッション key はすべて `state_json` 内にあり（スキーマ移行なし）、再試行ヘルパーは `agent/tools/taskflow/tools/_retry.py` にあります。

```python
# retry_policy on a step (stored by taskflow_run_task)
{"max_retries": 2, "retry_delay_seconds": 30.0, "retry_on": ["timeout", "rate_limit"]}
```

| フィールド | 型 | 既定値 | 意味 |
| :--- | :--- | :--- | :--- |
| `max_retries` | 非負整数 | `0` | **再**ディスパッチの最大回数 |
| `retry_delay_seconds` | 非負数値 | `60.0`（`DEFAULT_RETRY_DELAY_SECONDS`） | 各再ディスパッチ前にスリープするバックオフ |
| `retry_on` | `list[str]` | `[]` | 再試行を引き起こす失敗タイプ；空なら分類されたすべての失敗 |

`validate_policy()`（`_retry.py:120`）は不正なポリシーを `taskflow_run_task` の時点で拒否します——dict でないポリシー、負/非整数の `max_retries`、負/非数値の `retry_delay_seconds`、文字列リストでない `retry_on`——いずれもディスパッチや書き込みの前に `Error:` 文字列を返します。再開時、欠落/不正な保存済みポリシーは `None`（`normalize_policy`）へ退化し、呼び出しを失敗させる代わりに GAP-8 以前の再試行なし動作へ戻します。

`retry_count`（ステップに保存、既定 `0`）は**再ディスパッチ**の回数を数え、最初のディスパッチは数えません：最初の子が動いている間は `0`、最初の置換子が生成されると `1`。`retry_count < max_retries` の間は再試行が許可されます（`retries_remaining`、`_retry.py:95`）。`apply_redispatch()`（`_retry.py:170`）はステップをその場で変更します——新しい `child_session_key`、`dispatched_at`、`status = dispatched`、そして増分された `retry_count`。

### 失敗の分類

`classify_failure(result)`（`_retry.py:56`）は**結果テキストのヒューリスティック**です。テキストを小文字化し、失敗語を含むが成功を表す言い回し（`_NEGATED_FAILURE_PHRASES`、例：`"no error"`、`"error-free"`）を剥がしたうえで、最初に一致した順序付きバケットを返します：

| 分類タイプ | トリガー部分文字列 |
| :--- | :--- |
| `timeout` | `timeout`、`timed out`、`time limit` |
| `rate_limit` | `rate limit`、`rate_limit`、`429`、`too many requests` |
| `error` | `error`、`failed`、`failure`、`exception`、`traceback`、`aborted`、`crashed` |

クリーンな結果は決して再試行しません。`retry_on` が非空なら一致する分類タイプのみ再試行し、空なら分類されたすべての失敗が再試行されます（`should_retry_failure`、`_retry.py:103`）。

### 2 つの自動再ディスパッチ経路

| 入口 | トリガー | 動作 |
| :--- | :--- | :--- |
| `taskflow_wait_all` | ポーリング対象の子が結果なしで**死亡**確定 | `plan_settled_retries()` が予算の残る限り確定ステップごとに 1 回再ディスパッチし、尽きれば失敗ノート結果を付けて `done` にする |
| `taskflow_resume` | 注入された結果テキストが `retry_on` に許容される**失敗**と分類される | 置換子を生成し、失敗結果を記録し、ステップを新しい子の上で `dispatched` のまま保つ |

- **`taskflow_wait_all`** は各ターゲットの確定後に `_retry_settled_steps()`（`taskflow_wait_all.py:117`）を呼びます。ポリシーの無いステップは何のアクションも生みません（旧 flow は GAP-8 以前とバイト単位で同一の出力を保ちます）；結果がすでに注入済みの子はそのままにされます。1 回の呼び出しにつき確定ステップごとに**最大 1 つ**の再試行決定だけが実行されます——置換子が生成・記録され、オーケストレータが再び `wait_all` を呼んで待ちます。ツール内にバックグラウンド再試行ループは存在しません。置換子は `persist_retry_actions()`（`_retry.py:254`）で永続化され、`update_flow_with_conflict_retry()` を介して計画を読み直したステップ一覧へ再適用するため、並行書き込みが生成済みの置換子を落とすことはありません。
- **`taskflow_resume`** はステップを done にする前にそのポリシーを確認します（`taskflow_resume.py:122-144`）。再試行可能な失敗では `retry_delay_seconds` だけスリープして置換子を生成し、ステップは新しい子の上で `dispatched` のままです。置換子の生成自体が例外を投げた場合、ステップは失敗結果を付けて `done` のまま残り、応答にはその例外を名指しする `retry:` ノートが付きます。

### 枯渇と失敗ノート

`retry_count` が `max_retries` に達すると、`plan_settled_retries()` は再ディスパッチの代わりに `exhausted` アクションを発行します。ステップは `done` とされ、`retry_exhausted: True` を持つ失敗ノート結果が追記されます。これは `exhausted_note()` + `failure_result_record()` が生成します（`_retry.py:178-196`）：

```
retry budget exhausted: step step-4 child agent:main:session:... settled without a
result after 2 retry/retries (max_retries=2); marked done by taskflow_wait_all
```

枯渇したステップには明示的な判断が必要です——失敗ノートを再開するか、flow を失敗させるか。`wait_all` と `resume` の両経路は resume の冪等契約を守ります：失敗ノートは `result_hash` を持つため、再配達された確定が二重に記録されることはありません。

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

## ✅ 結果検証（GAP-7）

ステップは自然言語の**受け入れ基準**を持てるため、オーケストレータは子の結果を判断する具体的な根拠を得られます。両ツールが `validation_criteria` を受け取ります：

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:46
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,     # stored on the step
    retry_policy: dict | None = None,           # GAP-8 (see above)
    session_id: SessionId = "",
) -> str
```

`taskflow_run_task` は空白除去後に非空の `validation_criteria` をステップへ保存します（`blocked` と `dispatched` の両書き込み経路）。再開時、基準は強制されるのではなくオーケストレータへ**エコー**されます：

```python
# taskflow_resume.py:146-157
if step is not None and redispatched_key is None:
    step["status"] = str(StepStatus.DONE)
    criteria = (validation_criteria or "").strip()
    if criteria:
        step["validation_criteria"] = criteria
    stored_criteria = str(step.get("validation_criteria") or "").strip()
    if stored_criteria:
        validation_text = (
            f"\n  validation_criteria: {stored_criteria}"
            f"\n  ⚠ Result needs validation against criteria"
        )
```

ツールは決して基準を評価しません——注入された結果と並べて提示するだけであり、応答の末尾に `validation_criteria: …` と `⚠ Result needs validation against criteria` ブロックが付きます。重要なガードレールは 2 つです：

- `taskflow_resume` に `validation_criteria` を渡すと保存値が**上書き**されます（例えば子が実際に行った内容に基づいて基準を厳しくしたり訂正したりするため）。
- エコーはステップが実際に `done` とされる場合（`redispatched_key is None`）にのみ出ます；GAP-8 で再ディスパッチされたステップは基準を保存したままにし、最終的に成功した再開時にエコーを得ます。

判断者はオーケストレータ（メインエージェントモデル）です：子の結果を基準と比較し、そのステップを受け入れるか、再ディスパッチするか、flow を失敗させるかを決めます。自動の合否ゲートはありません。

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

## 📋 セッション横断ボード（GAP-9）

`taskflow_summary` は 1 つの flow を読み、自動再開の読み取りはセッションスコープです；`taskflow_list` は意図的にその逆——レジストリ全体を覆う**グローバルボード**であり、あるチャネル/チャットで開始した flow が他のどこからでも見えます：

```python
# agent/tools/taskflow/tools/taskflow_list.py:85
@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active") -> str
```

| `status_filter` | 行 |
| :--- | :--- |
| `"active"`（既定） | `running` + `waiting` のみ |
| `"all"` | 終端ステータスを含むすべての flow |
| その他の任意の値 | ステータスの完全一致（`running`、`waiting`、`done`、`failed`、`cancelled`） |

読み取り専用（`expected_revision` 不要）で、`store_sqlite.get_all_flows_sync(status_filter)`（`store_sqlite.py:566`）に支えられています。同期リーダーはイベントループを必要としない stdlib `sqlite3` パスを使い、行を `expected_revision DESC`（最近アクティブな順）で並べ、フェイルオープンです——初期化/読み取り失敗は `[]` を返します。`"active"` は `get_active_flows_sync()` へ委譲します。

描画されるボードは固定列のパディング済みテキスト表で、description は 40 文字、creator key は 16 文字に制限されます：

```
TaskFlow board (active): 2 flow(s)
flow_id | status  | description                              | steps | creator          | updated_at
--------+-..----+-..----------------------------------------+-..---+----------------+-..----------
flow-1  | running | Build the parser                         | 2/5   | agent:main:sess  | 2026-09-12 14:03:21
flow-2  | waiting | Wait for the upstream review              | 1/3   | agent:main:sess  | 2026-09-12 13:58:07
```

スキーマに **`updated_at` 列はありません**（GAP-9 は移行不要）。そのため `_last_activity_ts()`（`taskflow_list.py:28`）は「最終更新」を、flow 上のどこかに永続化された活動スタンプの最大値として導出します——`wait.set_at`、各 `step.dispatched_at`、各 `result.injected_at`——これを UTC タイムスタンプとして描画します（スタンプが全く無い flow は `-`）。空のレジストリは `No task flows found` を返します。

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

### 要約プロンプトの事実ベースライン（LT-3）

システムプロンプトは L2 の一行インデックスしか運ばないため、圧縮パスが、モデルがまだ必要とする事実へのポインタを要約で消してしまう可能性がありました。これを防ぐため、`_build_summary_prompt`（`agent/middlewares/summarization.py:1488-1506`）が `get_tiered_store().read_facts()` を通じて**非空のすべての事実**を読み、`<facts-baseline>` ブロックを要約プロンプトへ追記します：

```python
# summarization.py:1496
baseline_lines = ["<facts-baseline>"]
baseline_lines.append(
    "Persistent facts from tiered memory (ground truth, survives compression):"
)
for cat, content in non_empty.items():
    baseline_lines.append(f"[{cat}]")
    baseline_lines.append(content)
baseline_lines.append("</facts-baseline>")
parts.append("\n".join(baseline_lines))
```

このブロックは**グラウンドトゥルース**としてラベル付けされ、圧縮モデルがそれを捨てたり言い換えたりせず保持するようにします。LT-7 TaskFlow ブロックの後に追記され（どちらも LLM プロンプト専用の追加であり、`_build_static_fallback_summary` には現れません）、完全に**ベストエフォート**です：階層ストアの読み取り中の失敗は握りつぶされ（`except Exception: pass`）、圧縮を決してブロックしません。これはプロンプト層での再利用であり、L2 が何を保存するか、`memory` ツールがどう読むかは変えません。

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

## 🧠 サブエージェントメモリ還流（LT-5）

`SubagentCompletionDrainMiddleware`（`agent/middlewares/subagent_completion_drain.py`）は、キューに入ったサブエージェント完了メッセージの親ターン側の取り込み点です：`before_model` でセッションの `SteeringQueue` を再水和して排出し、再構築された完了キャリアメッセージを注入します。**排出が非空のとき**、共有メモリを親のインメモリビューと照合します：

```python
# subagent_completion_drain.py:68-93
def _backflow_shared_memory() -> None:
    from agent.tools.memory import memory_store
    memory_store.load_from_disk()
    for target in ("memory", "user"):
        memory_store.save_to_disk(target)
```

親と子は**単一のプロセス全体 `MemoryStore`** と同じ `facts/` ディレクトリを共有するため、子の書き込みはすでにファイル可視です。ドリフトしうるのは親のインメモリビュー——ライブエントリと、システムプロンプトの構築に使った**凍結スナップショット**——であり、これはプロセス外の書き手が `MEMORY.md` / `USER.md` を更新したときに起こります。**先に再読込**する順序が要です：古いインメモリ一覧を再読込前に永続化すると並行書き手を上書きしてしまうため、照合はターゲットごとに load → persist でなければなりません。

排出と同様、還流も**フェイルオープン**です——メモリ I/O の失敗はログに記録されて握りつぶされ、完了キャリアは親ターンへ届きます。また排出は内部完了キャリアに Sisyphus 検証リマインダーを追記し、完了は `DoneClaim` であって検証済み結果ではないことを親に思い出させます（todo を完了にする前に `todoread` で検証し、受け入れ基準に照らし、古い状態を調査します）。

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

すべての調整値は `config/features/` 配下にあり、これは**オブジェクト単位 `TypedDict` パッケージ**です——単一の巨大モジュールではありません。3 つの部分に分かれます：

| 部分 | 内容 |
| :--- | :--- |
| `config/features/agent_side/` | **20** 個のエージェント側設定モジュール（ミドルウェア、ツール、LLM クライアント、メモリ、TaskFlow） |
| `config/features/infra_side/` | **18** 個のインフラ側設定モジュール（サーバー、キュー、スキル、コンテキストエンジン、ランタイム、モデル価格） |
| `config/features/_env.py` | 唯一の共有環境ヘルパー |

各モジュールは `class XxxConfig(TypedDict)` とモジュールレベルの定数 `XXX: XxxConfig = {…}` を定義します。環境対応モジュールはビルダー `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig` を定義し、`env or os.environ` を読んでインポート時に定数を具体化します。環境ヘルパーは `_env_int(name, default, env)`（`config/features/_env.py:9`）で、`1/true/yes/on` と `0/false/no/off/""` を受け付け、決して例外を投げません。

レジストリは現在 **38 個の feature オブジェクト**を保持します——エージェント側 20 + インフラ側 18——各パッケージの `__init__.py` を通じて再エクスポートされ、`config/features/__init__.py` が集約するため、消費側は片方の半分またはレジストリ全体を 1 か所からインポートできます。消費側コードは定数をインポートして直接インデックスします（例：`ITERATION_BUDGET["default_max_iterations"]`）。`get_feature`/`load_feature` アクセサは存在しません。`config/__init__.py:38-39` は `GATEWAY` から `API_HOST`/`API_PORT` を導出します。

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
                    ┌────────────────────────────────────────────────────────┐
                    │              agent/wrapper/ registry                   │
                    │  apply_graph_wrappers() → innermost-first chain:       │
                    │  RepetitionGuardWrapper → ContextLimitGuardWrapper     │
                    └───────────────────────────┬────────────────────────────┘
                                                │ wraps the compiled graph
                          ┌─────────────────────▼────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (13, main_only)   │          │  add/fact_add/…      │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │ TieredMemoryStore(L2)│                 │ ─ FACTS index (L2)     │
│ state_json DAG    │          │  agent/tools/        │                 │ ─ Pending TaskFlows    │
│ + retry/validation│          │  memory_tiered.py    │                 │ ─ Last Session         │
└────────┬──────────┘          └──────────────────────┘                 └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
         │                                                │                        │
         │ drain (LT-5)                                   │                        │
         ▼                                                │                        │
┌──────────────────────────────┐                          │                        │
│ SubagentCompletionDrain      │                          │                        │
│ _backflow_shared_memory      │                          │                        │
└──────────────────────────────┘                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+LT-3 facts,     │   │
└──────────────────────────────┘                   │  +LT-7 TaskFlow)          │   │
                                                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

コンパイル済みグラフはもはや `agent.core.py` 内でインラインにラップされません：**`agent/wrapper/`** パッケージがガードを所有します。`agent.wrapper.registry` はプロセス全体の、順序付きでプラグ可能なチェーン（`register_graph_wrapper`、`unregister_graph_wrapper`、`apply_graph_wrappers`、`reset_graph_wrappers`）を公開し、`GraphWrapperFactory` エントリは**最内優先**で適用されます；既定値は歴史的なハードコードチェーンを再現します——まず `RepetitionGuardWrapper(phantom_stream_guard=True)`、次に `ContextLimitGuardWrapper(context_window=main_llm_max_tokens)`。ストリーム重複ガードは `agent/wrapper/repetition_guard.py`、コンテキストウィンドウガードは `agent/wrapper/context_limit.py` にあります。`facts/` 層を支える **TieredMemoryStore（L2）** は `agent/tools/memory_tiered.py` にあり、**LT-5** 還流は `agent/middlewares/subagent_completion_drain.py` の `SubagentCompletionDrainMiddleware` が実行します。

## 📚 API リファレンス

### TaskFlow ツール

| ツール | シグネチャ | 戻り値 |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | 作成した id/ステータス/リビジョン（+ 締め切り） |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, validation_criteria=None, retry_policy=None, session_id)` | ディスパッチ済みステップ、または未充足依存付き `blocked` |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | ディスパッチ済み step id + リビジョン |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | ステップごとの確定レポート（完全または部分；ポリシー付きステップを自動再試行） |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None, validation_criteria=None, session_id)` | 再開後ステータス、アンロック済みステップ、ステップ状態カウント、基準エコー、再試行ノート |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None)` | waiting ステータス + リビジョン |
| `taskflow_summary` | `(flow_id)` | 待機/締め切り状態を含む完全な flow 状態 |
| `taskflow_progress` | `(flow_id)` | 完了率、内訳、次のステップ、残り推定 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None)` | 予算レポート、または設定確認 |
| `taskflow_list` | `(status_filter="active")` | グローバルなセッション横断ボード（`active` / `all` / ステータス名） |
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
| `get_all_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py:566` | セッション横断ボード読み取り |
| `classify_failure` / `should_retry_failure` | `agent/tools/taskflow/tools/_retry.py:56,103` | GAP-8 失敗分類 |
| `plan_settled_retries` / `persist_retry_actions` | `agent/tools/taskflow/tools/_retry.py:199,254` | GAP-8 wait_all 再試行の計画/永続化 |
| `get_tiered_store` | `agent/tools/memory_tiered.py:118` | L2 事実ストア + LT-3 ベースライン源 |
| `_backflow_shared_memory` | `agent/middlewares/subagent_completion_drain.py:68` | LT-5 メモリ還流の照合 |
| `apply_graph_wrappers` | `agent/wrapper/registry.py:69` | プラグ可能なグラフラッパーチェーン |

## 🧪 テスト

TaskFlow スイートは `tests/agent/tools/taskflow/` にあります（17 個の `unit` テストファイル + 共有 `conftest.py`）：

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
| `test_retry_policy.py` | GAP-8 ポリシー検証、失敗分類、再ディスパッチ、枯渇 |
| `test_validation.py` | GAP-7 基準の保存、再開エコー、上書き |
| `test_taskflow_list.py` | GAP-9 ボード描画、ステータスフィルタ、最終活動タイムスタンプ |

横断スイート：`tests/agent/middlewares/test_memory_flush.py`（フラッシュ閾値と `append_entries`）、`tests/agent/middlewares/test_lt5_memory_backflow.py`（完了排出時の LT-5 メモリ照合）、`tests/agent/middlewares/test_subagent_completion_drain_reminder.py`（完了キャリア検証リマインダー）、`tests/agent/tools/test_memory_tiered.py`（階層事実）、`tests/context_engine/test_session_continuity.py`（継続性の保存/プロンプト）、`tests/agent/middlewares/test_todo_continuation.py`（ターン終了時の継続）、`tests/pub/func/message/test_tool_output_prune.py`（一行要約）、`tests/workspace/test_prompt_builder_taskflow.py`（保留 flow のプロンプト注入）。

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
- **結果検証は助言的。** `validation_criteria` は保存され結果と共にエコーされますが、ツールが強制することはありません；合否はオーケストレータ自身が判断する必要があります。基準未達でステップを失敗させられる自動ゲートはありません。
- **再試行分類はテキストベース。** `classify_failure` は結果テキストに対する部分文字列ヒューリスティックです：パターン表の外の言い回しの失敗（または否定フレーズに隠れた真の失敗）は再試行を引き起こさず、空の `retry_on` は分類されたすべての失敗を再試行します。`taskflow_wait_all` は結果テキストの無い死亡した子を分類できないため、予算が残る限り常に再試行予算を消費します。
- **`taskflow_list` は意図的にグローバル。** セッション横断ボードは `creator_session_key` スコープを無視するため、任意のメインエージェントセッションがレジストリ内のすべての flow を列挙できます（読み取り専用、`expected_revision` なし）。セッション単位のビューではありません。
- **事実ベースラインは LLM プロンプト専用の追加。** LT-3 の `<facts-baseline>` ブロックは `_build_summary_prompt` が追記し、決定論的フォールバック要約には現れません——LT-7 TaskFlow ブロックと全く同じです。
