# 🧩 TaskFlow エンジン — DAG、再試行、予算、締め切りとボード

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Long-Running Tasks](../README.ja.md) の一部：永続 DAG エンジン、ステップ再試行ポリシー、トークン/コスト予算、締め切り、結果検証、進捗レポート、アイドル検出、セッションボード。

---

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
    deadline_ts REAL,
    session_id TEXT NOT NULL DEFAULT ''
);
```

DAG 自体（`steps[]`、`results[]`、`depends_on`、`creator_session_key`）は完全に `state_json` の中にあります——DAG フィールドの追加にスキーマ移行は不要です。トークン/コスト/締め切りの列は追加的 DDL（`_TOKEN_COLUMN_DDL`、`_DEADLINE_COLUMN_DDL`、`_SESSION_ID_COLUMN_DDL`、`store_sqlite.py:83-129`）で定義されます。`session_id` は分離列です（`idx_taskflow_session_status` が索引）：作成時に刻まれ、以後のすべての読み取り/変更がそれをフィルタします（`WHERE flow_id = ? AND session_id = ?`、`WHERE session_id = ? AND status IN (…)`）。WAL プラグマはプロセスごとに一度だけ切り替えられ、すべてのステートメントの前に `PRAGMA busy_timeout = 5000` が実行されます（`store_sqlite.py:234-271`）。

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

`done` の意味は**「結果が注入された」**であり、「子が成功した」ではありません（[既知の制限](../README.ja.md#-既知の制限)を参照）。`status` フィールドを持たない旧ステップは導出されます：`child_session_key` を持つステップは `dispatched`、それ以外は `ready` とみなされます（`_shared.py:85`、`step_status`）。

### `depends_on` の意味論

`deps_satisfied(step, steps)`（`_shared.py:99`）は、すべての `depends_on` id が現在のステップ一覧に存在し**かつ** `done` である場合にのみ真です。`depends_on` が欠落/空なら自明に満たされます。未知の依存 id は決して満たされず、**自己依存も決して満たされません**——そのため自己参照ステップはアンロックループに陥らず安全にブロックされ続けます。`unlock_dependents(steps)`（`_shared.py:137`）はリストを**一度だけ走査**するため、依存サイクルがループすることを構造的に防ぎます。`taskflow_run_task` はディスパッチや状態変更の**前に**未知の依存 id を拒否します。

### ツールファミリ（14 ツール）

すべてのツールは `async` で、`@tool("taskflow_…")` としてデコレートされ、`build_taskflow_tools()`（`tools/__init__.py:58-76`）が `metadata={"scope": "main_only"}` と `handle_tool_error=True` を付与します。共有 flow 状態を管理できるのはメインエージェントだけであり、サブエージェントのツールポリシーはファミリ全体を無条件に除外します。

| ツール | 目的 |
| :--- | :--- |
| `taskflow_create` | リビジョン 1 で flow を作成；任意で `deadline_hours` を設定 |
| `taskflow_run_task` | ステップを登録（任意で `validation_criteria` / `retry_policy`）してディスパッチ（または `blocked` として記録） |
| `taskflow_dispatch` | 複数の準備完了ステップをオール・オア・ナッシングで一括ディスパッチ |
| `taskflow_update_steps` | steps リストを全置換（追加/削除/並べ替え/task・depends_on の書き換え；dispatched/done の安全規則；孤児 child 警告） |
| `taskflow_wait_all` | flow スコープの有界ポーリングでディスパッチ済みステップの確定を待つ（ポリシー付き確定ステップを自動再試行） |
| `taskflow_resume` | 子の結果を冪等に注入し、後続をアンロックし、トークンを集計（失敗認識の再試行 + 基準エコー） |
| `taskflow_set_waiting` | 理由付きで flow を `waiting` に停める |
| `taskflow_summary` | 読み取り専用の再読込（競合後の再読込にも使う） |
| `taskflow_progress` | 人間可読な進捗/完了レポート |
| `taskflow_budget` | トークン/コスト予算の照会または設定 |
| `taskflow_list` | このセッションのボード（`active` / `all` / ステータス名） |
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

### 更新——`taskflow_update_steps` の全置換

`taskflow_update_steps(flow_id, steps, expected_revision=None, session_id="")`（`taskflow_update_steps.py:191`）は flow の steps リストを全置換します（TaskFlow にとっての `todowrite`）：保存される DAG は渡したリストそのものになり、ステップの追加・削除・並べ替え・`task`/`depends_on` の書き換えができます。安全規則：`step_id` は一意で、各 `depends_on` は新しいリスト内に存在する id を参照する必要があります（自己依存は禁止）。`dispatched` ステップは `child_session_key` を保持し、`ready`/`blocked` へ降格できません。`done` ステップは task/depends_on/status を変更できません。新規ステップは `ready`/`blocked` のみ（ディスパッチは `taskflow_dispatch` 経由）。終端 flow は呼び出しを拒否します。実行中の `dispatched` ステップを削除すると成功しますが、非ブロッキングの `Warning:`（child key 付き）を返します——先に child を kill するか、`taskflow_wait_all`/`taskflow_resume` で確定させてください。並行性は同じ楽観的ロックを用い、`expected_revision` の不一致は最新リビジョンとともに拒否され、再読込と再試行に使えます。

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

## 🔁 ステップ再試行ポリシー

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

`validate_policy()`（`_retry.py:120`）は不正なポリシーを `taskflow_run_task` の時点で拒否します——dict でないポリシー、負/非整数の `max_retries`、負/非数値の `retry_delay_seconds`、文字列リストでない `retry_on`——いずれもディスパッチや書き込みの前に `Error:` 文字列を返します。再開時、欠落/不正な保存済みポリシーは `None`（`normalize_policy`）へ退化し、呼び出しを失敗させる代わりに再試行なし動作へフォールバックします。

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

- **`taskflow_wait_all`** は各ターゲットの確定後に `_retry_settled_steps()`（`taskflow_wait_all.py:117`）を呼びます。ポリシーの無いステップは何のアクションも生みません（旧 flow はバイト単位で同一の出力を保ちます）；結果がすでに注入済みの子はそのままにされます。1 回の呼び出しにつき確定ステップごとに**最大 1 つ**の再試行決定だけが実行されます——置換子が生成・記録され、オーケストレータが再び `wait_all` を呼んで待ちます。ツール内にバックグラウンド再試行ループは存在しません。置換子は `persist_retry_actions()`（`_retry.py:254`）で永続化され、`update_flow_with_conflict_retry()` を介して計画を読み直したステップ一覧へ再適用するため、並行書き込みが生成済みの置換子を落とすことはありません。
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

## ✅ 結果検証

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
    retry_policy: dict | None = None,           # (see above)
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
- エコーはステップが実際に `done` とされる場合（`redispatched_key is None`）にのみ出ます；再試行ポリシーで再ディスパッチされたステップは基準を保存したままにし、最終的に成功した再開時にエコーを得ます。

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

## 📋 セッションボードと分離

各セッションは自分のデータだけを見ます。`taskflow_summary`、自動再開の読み取り、そして `taskflow_list` もすべて `session_id` でスコープされ、ストアが SQL 層でフィルタします——他のセッションの flow は存在しない flow と区別できません（変更時は `FlowNotFoundError`、読み取り時は `None`）：

```python
# agent/tools/taskflow/tools/taskflow_list.py:85
@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active", session_id: SessionId = "") -> str
```

| `status_filter` | 行 |
| :--- | :--- |
| `"active"`（既定） | このセッションの `running` + `waiting` |
| `"all"` | このセッションの flow（終端を含む） |
| その他の任意の値 | ステータスの完全一致（`running`、`waiting`、`done`、`failed`、`cancelled`） |

読み取り専用（`expected_revision` 不要）で、`store_sqlite.get_all_flows_sync(session_id, status_filter)` に支えられています。同期リーダーはイベントループを必要としない stdlib `sqlite3` パスを使い、行を `expected_revision DESC`（最近アクティブな順）で並べ、フェイルオープンです——初期化/読み取り失敗は `[]` を返します。`"active"` は `get_active_flows_sync(session_id)` へ委譲します。

描画されるボードは固定列のパディング済みテキスト表で、description は 40 文字、creator key は 16 文字に制限されます：

```
TaskFlow board (active): 2 flow(s)
flow_id | status  | description                              | steps | creator          | updated_at
--------+-..----+-..----------------------------------------+-..---+----------------+-..----------
flow-1  | running | Build the parser                         | 2/5   | agent:main:sess  | 2026-09-12 14:03:21
flow-2  | waiting | Wait for the upstream review              | 1/3   | agent:main:sess  | 2026-09-12 13:58:07
```

スキーマに **`updated_at` 列はありません**（移行不要）。そのため `_last_activity_ts()`（`taskflow_list.py:28`）は「最終更新」を、flow 上のどこかに永続化された活動スタンプの最大値として導出します——`wait.set_at`、各 `step.dispatched_at`、各 `result.injected_at`——これを UTC タイムスタンプとして描画します（スタンプが全く無い flow は `-`）。空のレジストリは `No task flows found` を返します。

### セッション所有の 3 つの計画ツールファミリ

| ファミリ | セッション紐付け | ストア |
| :--- | :--- | :--- |
| **TaskFlow** | `task_flows.session_id` 列（今回の変更） | `agent/tools/taskflow/registry/store_sqlite.py` |
| **TodoList** | `session_id` がテーブルの主キー接頭辞——元からセッションスコープ | `agent/tools/todolist/registry/store_sqlite.py` |
| **Knowledge** | 計画アイデンティティ単位で分離（ツール層で強制）：関連は `ownership.association_plan_refs()`（セッションの `plan_ref` 状態キー、セッション todo の `plan_ref`（SQL で `session_id` をフィルタ）、`src/data/boulder.json` のうち `plan_name` が一致し `session_ids` に当該セッションを含む work）から取得し、`identity.resolve_plan_identity()` が名前を正規化された計画パスへ解決して保存キー `sha1(リポジトリルート相対パス)[:12]` を導出します。別ファイルの同名計画は物理的に隔離され、boulder `session_ids` で共有された 1 つの計画ファイルは列挙された全セッションで同じ `<plan_key>` に解決されます。非関連・曖昧な計画への `read` / `write` は診断可能なエラーで拒否され、`list` は関連付けられた計画のみ（可読名 + key）を返します；`build_knowledge_block(session_id)` はプロンプトブロック用にセッションの主アイデンティティを解決します；`clear_session` はセッション私有のアイデンティティディレクトリを削除し、他セッションと共有された計画は保持します。 | `agent/tools/todolist/knowledge/identity.py` |

**サブエージェント境界。** 3 ファミリはいずれもビルダー（`build_taskflow_tools`、`build_todolist_tools`、`build_knowledge_tools`）が `metadata["scope"] = "main_only"` を付与します。`apply_tool_policy`（`agent/tools/subagent/spawn/inherited_tool_policy.py`）は `main_only` ツールを**最初に無条件で**落とします——allow/deny リストより先で、ORCHESTRATOR の解除でも上書きできません——したがって spawn された子エージェントが `taskflow_*`、`todowrite`/`todoread`、`knowledge` を受け取ることは決してありません。同じタグパターンは既に `memory`、`skill_manage`、`sessions_kill`、`sessions_steer` を覆っています。実ツールセットの表明は `tests/agent/tools/taskflow/test_taskflow_tools.py`、`_build_child_agent` 境界は `tests/agent/tools/subagent/test_max_tokens_boost_wiring.py` が固定します。

**セッション間拒否。** `taskflow_create` で他セッションが使用中の `flow_id` に衝突した場合、存在だけを報告しリビジョンは漏らしません；他セッションの flow への変更は未知 id と同じ "not found" テキストを返します。読み取り/一覧/更新/パージ経路は `tests/agent/tools/taskflow/test_store_sqlite.py`、`test_taskflow_tools.py`、`test_dag_e2e.py`、`tests/server/DAO/test_clear_session.py` がカバーします。


