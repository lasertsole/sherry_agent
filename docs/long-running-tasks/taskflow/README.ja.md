# TaskFlow — DAG 依存関係を持つ永続的マルチステップタスクフロー

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> TaskFlow は、SQLite（楽観的ロック、WAL モード）に基づくターン間永続タスクフロー管理システムです。主な機能：分離されたサブエージェントステップのディスパッチ、DAG ベースの依存関係管理、バッチ並列ディスパッチ、有界ポーリング待機、べき等結果注入。14 個のツールが完全なライフサイクル API を構成し、openclaw managedFlows インターフェースと対応します：`taskflow_create` → `taskflow_run_task` → `taskflow_dispatch` / `taskflow_wait_all` → `taskflow_resume` → `taskflow_finish` / `taskflow_fail` / `taskflow_cancel`、加えて `taskflow_summary`（読み取り専用再読み）、`taskflow_set_waiting`（待機状態への移行）、`taskflow_progress`（進捗レポート）、`taskflow_budget`（トークン/コスト予算）、`taskflow_update_steps`（ステップリストの完全置換）、`taskflow_list`（セッションボード）。

信頼できる情報源：`agent/tools/taskflow/tools/*.py`、`agent/tools/taskflow/registry/store_sqlite.py`、`agent/tools/taskflow/config.py`。スキルリファレンス：`skills/builtin/core/taskflow/SKILL.md`。

---

## 目次

- [概要](#概要)
- [アーキテクチャ](#アーキテクチャ)
- [状態機械](#状態機械)
- [ツールファミリー（14 個のツール）](#ツールファミリー14-個のツール)
- [楽観的ロックと競合リトライ](#楽観的ロックと競合リトライ)
- [DAG 依存関係システム](#dag-依存関係システム)
- [並列ステップ実行](#並列ステップ実行)
- [べき等リジューム](#べき等リジューム)
- [生成済み子エージェントを伴う競合リトライ](#生成済み子エージェントを伴う競合リトライ)
- [永続化と接続ライフサイクル](#永続化と接続ライフサイクル)
- [既知の制限](#既知の制限)

---

## 概要

TaskFlow（`agent/tools/taskflow/`）は、SQLite（WAL モード）上に構築された永続的タスクフローシステムで、複数の会話ターンにまたがるマルチステップ作業を管理します。各フローはライフサイクル（running → waiting → done/failed/cancelled）を持ち、ステップは互いに依存関係を宣言でき、結果は分離された子エージェントセッションから注入されます。

主要な設計方針：

- **楽観的ロック**：全変更操作は `expected_revision` を 1 ずつ増分。リビジョン競合により同時書き込みを検出（最後の書き込み優先ではない）。
- **state_json 内の DAG**：ステップ依存関係（`depends_on`）、ステータス、結果はすべてフローの `state_json` カラム内に格納——DAG 機能に DB スキーマ移行は不要。
- **分離されたディスパッチ**：ステップは既存の spawn パイプラインを通じて分離された子エージェントセッションを spawn して実行。結果は announce/settle-wake パイプラインを通じて還流。
- **べき等リジューム**：`(child_session_key, result)` ペアをフィンガープリント化。重複配信は二度注入されない。
- **エラー即テキスト契約**：ツールは LLM に業務エラーを raise しない。`Error:` プレフィックス付きの人間可読文字列を返す。

---

## アーキテクチャ

```
agent/tools/taskflow/
├── __init__.py              # パッケージエクスポート（11 ツール再エクスポート）
├── config.py                # TaskFlowStatus、StepStatus 列挙型、TERMINAL_STATUSES、TABLE_NAME
├── step_judge.py            # StepJudge — 補助 LLM の pass/retry/block 判定器
├── evidence_collector.py    # 判定プロンプト用のセッション/フロー evidence 要約を描画
├── registry/
│   ├── __init__.py
│   └── store_sqlite.py      # SQLite 永続化：create/get/update、WAL、busy_timeout、
│                            #   FlowConflictError/FlowNotFoundError/FlowExistsError、
│                            #   同期パス（get_flow_sync）
└── tools/
    ├── __init__.py           # build_taskflow_tools() → 14 ツール、scope=main_only
    ├── _dispatch.py          # monkeypatch 可能なディスパッチシーム（spawn_subagent_direct）
    ├── _shared.py            # DAG ヘルパー + 競合リトライ永続化
    ├── taskflow_create.py    # フロー作成、初期リビジョン 1
    ├── taskflow_run_task.py  # ステップ登録 + ディスパッチ（または依存未満足でブロック）
    ├── taskflow_dispatch.py  # 複数の準備完了ステップをバッチディスパッチ
    ├── taskflow_wait_all.py  # ディスパッチ済みステップの完了を有界ポーリング待機
    ├── taskflow_resume.py    # 結果注入、完了マーク、後続アンロック（べき等）
    ├── taskflow_set_waiting.py # フローを waiting 状態に移行
    ├── taskflow_summary.py   # 読み取り専用再読み（競合後の再読みにも使用）
    ├── taskflow_progress.py  # 読み取り専用の進捗レポート
    ├── taskflow_budget.py    # トークン/コスト予算の照会と設定
    ├── taskflow_update_steps.py # ステップリストの完全置換
    ├── taskflow_list.py      # セッション単位のフローボード
    ├── taskflow_finish.py    # 完了マーク（終端状態）
    ├── taskflow_fail.py      # 失敗マーク（終端状態）
    └── taskflow_cancel.py    # フロー取消（終端状態）
```

### 登録

ツールは `agent/tools/taskflow/tools/__init__.py` の `build_taskflow_tools()` で登録され、全 14 ツールを `metadata = {"scope": "main_only"}` および `handle_tool_error = True` タグ付きで返します。サブエージェントのツールポリシーはこれらを無条件で破棄——メインエージェントのみが共有フロー状態を管理します。

---

## 状態機械

### フロー状態（`TaskFlowStatus`）

```
running ──→ waiting ──→ running（taskflow_resume 経由）
  │                       └──→ done    （taskflow_finish、終端）
  │                       └──→ failed  （taskflow_fail、終端）
  │                       └──→ cancelled（taskflow_cancel、終端）
  └──→ done / failed / cancelled（running から直接）
```

終端状態（`done`、`failed`、`cancelled`）は不変：全変更ツールは状態変更前に `is_terminal(status)` をチェックし、`Error: TaskFlow '<id>' is terminal (status=...); no further mutations allowed` を返します。

### ステップ状態（`StepStatus`）

```
blocked → ready → dispatched → done
```

| 状態         | 意味                                                                             | 遷移トリガー                                   |
| ------------ | -------------------------------------------------------------------------------- | ---------------------------------------------- |
| `blocked`    | 依存関係がまだすべて `done` ではない；`taskflow_run_task` は登録のみ、spawn なし | `taskflow_resume` が依存関係満了時にアンロック |
| `ready`      | 依存関係満た済み、ディスパッチ待ち                                               | `taskflow_dispatch` がディスパッチ             |
| `dispatched` | 分離された子セッションが spawn 済み、`child_session_key` 永続化済み              | `taskflow_resume` が結果注入                   |
| `done`       | 結果が `taskflow_resume` で注入済み                                              | （このステップの終端状態）                     |

> `done` は「結果が注入された」ことを意味し、**「子エージェントが成功した」ことを意味しない**。`failed`/`skipped` ステップ状態は存在せず、失敗対応はステップのオプトイン `retry_policy` とステップ判定器（`block` 判定または再試行予算の枯渇で `blocked`）にあります。

### 従来ステップの互換性

`status` フィールドを持たないステップは `step_status()` で処理：`child_session_key` を持つステップは `dispatched`、それ以外は `ready` として扱われます。フィールドを持たないフローとの後方互換性を保ちます。

---

## ツールファミリー（14 個のツール）

### taskflow_create

```python
async def taskflow_create(flow_id: str, description: str = "", initial_state: dict | None = None) -> str
```

永続的フローを `INITIAL_REVISION = 1`、状態 `running` で作成。フロー id、状態、リビジョンを返します。id が既に使用中の場合 `FlowExistsError`（現在のリビジョンを含むエラー文字列を返し、再読み取り可能）。

### taskflow_run_task

```python
async def taskflow_run_task(
    flow_id: str, task: str, label: str | None = None,
    depends_on: list[str] | None = None,
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

フローにステップを登録し、分離された子エージェントにディスパッチ。ステップ id は `step-1`、`step-2` 等に自動採番。

- **依存関係なし**（または全て満た済み）→ ステップは即座に `dispatched`（子エージェント spawn）。
- **依存関係未満足** → ステップは `blocked` として登録、子エージェントは spawn されない。未知の依存 id は spawn や状態変更の前に拒否。
- `step_id`、`child_session_key`、`revision` を返す（ブロック時は `pending=[...]` に未満足依存をリスト）。

### taskflow_dispatch

```python
async def taskflow_dispatch(
    flow_id: str, step_ids: list[str],
    expected_revision: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

1 つ以上の現在準備完了ステップをバッチディスパッチ。ステップが `ready` の場合、または `blocked` だが依存関係が満たされた場合にディスパッチ可能。

- **全か無かの検証**：全ての id を spawn 前に検証。未知の id、ディスパッチ済み/完了ステップ、依存未満足のブロックステップは呼び出し全体を拒否、spawn ゼロ。
- **バッチ途中失敗**：spawn がバッチ途中で失敗した場合、成功した spawn は 1 回の `update_flow` で永続化し、子エージェントを欠落させない。エラーは失敗およびディスパッチ済みステップ id を明示。
- フロー級の `child_session_key` は意図的に変更しない——ステップ毎の child key が権威ソース。

### taskflow_wait_all

```python
async def taskflow_wait_all(
    flow_id: str, timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 0.5,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

このフローのディスパッチ済み子セッションが完了するまで（またはタイムアウト）待機。このフローのディスパッチ済みステップに記録された子セッションのみをポーリング——無関係なバックグラウンド子エージェントは戻りをブロックしない。

- 未知/不在の run は完了済みとして扱う（ハングしない）。
- タイムアウト時、部分レポートを返し、完了した子エージェントを `taskflow_resume` で処理して再度 `wait_all` を呼ぶよう指示。
- レジストリシーム（`get_run_by_child_session_key` / `is_live_unended_run`）は遅延インポートかつ注入可能でテスト容易化。

### taskflow_resume

```python
async def taskflow_resume(
    flow_id: str, child_session_key: str = "", result: str = "",
    expected_revision: int | None = None, token_usage: dict | None = None,
    validation_criteria: str | None = None,
) -> str
```

完了した子セッションの結果をフロー状態に注入（べき等）。`{child_session_key, result, result_hash, injected_at}` を results に追加。フローが `waiting` だった場合、`running` に復帰。

**DAG 記簿**：マッチするステップを `done` にマークし、`unlock_dependents()` を呼んで新たに満たされた `blocked` ステップを `ready` に移行。アンロックされたステップ id を返す。**resume は決して spawn しない**——呼び出し側は `taskflow_dispatch` で新たに準備完了したステップを明示的にディスパッチする必要がある。

**ステップ判定器**：ステップが `validation_criteria` を持つ場合（`taskflow_run_task` が設定、またはここで渡す）、補助 LLM 判定器（`agent/tools/taskflow/step_judge.py`、温度 0）が結果を基準に照らして審査し、`pass` / `retry` / `block` を返す。`retry` は共有 `_retry` シームを通じてステップを再ディスパッチし、ステップ自身の `retry_count` 予算（`STEP_JUDGE["max_retries"]`、既定 2）を再利用して、判定器の指針を `with_judge_feedback` で代替タスクに追加する。予算を使い切るか `block` 判定の場合は、判定器の理由とともにステップを `blocked` にする。判定器にはこのフローの evidence 要約が渡され、フェイルオープンである — 判定器の無効化、モデルエラー、解析不能な応答は `pass` に劣化する。

### taskflow_set_waiting

```python
async def taskflow_set_waiting(
    flow_id: str, wait_reason: str = "",
    expected_revision: int | None = None,
) -> str
```

フローを `waiting` 状態に移行し、待機理由を記録。待機ペイロードは `taskflow_resume` でクリア。

### taskflow_summary

```python
async def taskflow_summary(flow_id: str) -> str
```

読み取り専用でフロー状態を全て再読み：状態、リビジョン、child_session_key、説明、全ステップ（状態、depends_on、child_session_key 付き）、ステップ状態カウント、結果、待機ペイロード、サマリー、失敗理由、取消理由。リビジョン競合後の指定再読みステップでもある。

### taskflow_progress

```python
async def taskflow_progress(flow_id: str) -> str
```

読み取り専用の完了レポート：完了率、ステータス内訳、次のステップ、推定残り時間（`dispatched_at` タイムスタンプを持つ `done` ステップが 2 つ以上ある場合）。フローを変更しません。

### taskflow_budget

```python
async def taskflow_budget(
    flow_id: str, action: str = "query", token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

フローのトークン/コスト予算を照会（`query`）または設定（`set`）します。`query` は `total_tokens`、`total_cost`、予算、残りトークン、ステータス（`ok` / 80% で `WARNING` / `EXCEEDED`）を報告し、`set` は正の `token_budget` を要求して楽観的ロック経由で書き込みます。

### taskflow_update_steps

```python
async def taskflow_update_steps(
    flow_id: str, steps: list[dict],
    expected_revision: int | None = None,
) -> str
```

フローのステップリストを完全置換します（TaskFlow における `todowrite` 相当）：ステップの追加・削除・並べ替え、`task`/`depends_on` の書き換えが可能です。安全規則により `dispatched` ステップは `child_session_key` に束縛されたままとなり、`done` ステップの書き換えは拒否されます。子がまだ実行中の `dispatched` ステップを削除すると成功しますが、子キーを示す非ブロッキングの `Warning:` を返します。

### taskflow_list

```python
async def taskflow_list(status_filter: str = "active") -> str
```

このセッションのフローの読み取り専用ボード：`"active"`（running + waiting）、`"all"`（終端ステータスを含む）、または正確なステータス名。すべての読み取りは所有する `session_id` で SQL フィルタされ、レンダリング表は説明を 40 文字に制限し、フローのアクティビティタイムスタンプから `updated_at` を導出します。

### taskflow_finish / taskflow_fail / taskflow_cancel

```python
async def taskflow_finish(flow_id: str, summary: str = "", expected_revision: int | None = None,
                          todo: dict | None = None, plan_path: str | None = None,
                          checkbox_label: str | None = None) -> str
async def taskflow_fail(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
async def taskflow_cancel(flow_id: str, reason: str = "", expected_revision: int | None = None) -> str
```

終端遷移。`finish` は `summary`、`fail` は `failure_reason`、`cancel` は `cancel_reason` をフロー状態に記録。ディスパッチ済みの子セッションは実行を継続し、フローが取消される前の結果は `taskflow_resume` で配信可能。

`finish` は DONE 遷移の前に 4 つのゲートを順に通す：**A** すべてのステップが `done` または `blocked`；**B** `blocked` のステップが存在しない；**C** このフローの evidence（`agent/tools/taskflow/evidence_collector.py`）に `FAIL` 行も `[stale]` 行もない；**D** `SisyphusVerifier` が合格 — 呼び出し側が `todo` と `plan_path` の両方を明示的に渡した場合のみで、フロー/ステップの schema 移行は不要。すべてのゲートはフェイルオープン：evidence コレクタが利用不能、または検証器エラーの場合は完了をブロックせず通過する。

---

## 楽観的ロックと競合リトライ

全変更操作は `UPDATE ... WHERE flow_id = ? AND expected_revision = ?` で実行。更新がゼロ行にマッチした場合、書き込みが競合した（またはフローが消失した）：

- **`FlowConflictError`**：最新リビジョンを保持し、呼び出し側が再読みしてリトライ可能。エラーテキストに埋め込まれた値を直接使用可能：`expected_revision=2 but latest revision=3; re-read with taskflow_summary and retry with expected_revision=3`。
- **`FlowNotFoundError`**：フローが削除された。

**リトライフロー**（LLM 視点）：

1. `taskflow_summary(flow_id)` を呼んで再読みし、最新 `revision` を取得。
2. `expected_revision=<最新リビジョン>` で変更を再適用。
3. 競合エラーテキストに直接使用可能なリトライ値が含まれる。

終端フローは不変：全変更ツールは状態変更前に `is_terminal(status)` をチェック。

---

## DAG 依存関係システム

DAG フィールドは完全に `state_json` 内に存在（DB 移行不要）。`StepStatus` 列挙型が 4 状態を定義し、`_shared.py` の 3 つの純粋関数が遷移を管理：

### deps_satisfied(step, steps) → bool

全ての `depends_on` id が `steps` 内に存在し `done` の場合に True。`depends_on` が不在/空は自明に満たされる。未知の dep id は決して満たされない。**自己依存は決して満たされず**、自己参照ステップを安全に blocked のまま保持し、無限アンロックループを生じない。

### mark_step_done(steps, child_session_key) → str | None

`child_session_key` にマッチするステップを `done` にマーク。べき等：同一 key への重複呼び出しは同じステップ id を返す。該当する child key を持つステップがない場合は `None` を返す。

### unlock_dependents(steps) → list[str]

`steps` を**シングルパス**で走査：依存関係が満たされた `blocked` ステップを `ready` に移行。新たに準備完了したステップ id を返す。シングルパスにより依存サイクルが無限ループを生じないことを保証。

---

## 並列ステップ実行

2 つのツールが独立ステップの並列実行を可能に：

### バッチディスパッチ（`taskflow_dispatch`）

全 spawn の前にバッチ全体を検証（全か無か）。共有の `_dispatch.dispatch_child` シームを通じて順次 spawn。バッチ途中の spawn 失敗時はバッチを停止し、成功した spawn を 1 回の `update_flow` で永続化。`apply_dispatched_steps()` ヘルパーが新読みのステップリストに記録済みディスパッチステップペイロードを再適用し、`step_id` でマッチ——spawn 済み子エージェントが同時書き込みによるステップリスト書き換えで失われることはない。

### 有界ポーリング待機（`taskflow_wait_all`）

フロースコープ：このフローのディスパッチ済みステップに記録された子セッションのみをポーリング。無関係なバックグラウンド子エージェントでブロックしない。ポーリング間隔に下限設定（最小 `0.05s`）。未知/不在の run は完了済みとして扱う（ハングしない）。タイムアウト時は部分レポートを返す。

---

## べき等リジューム

`taskflow_resume` は `(child_session_key, result)` ペアを SHA-256 で 16 ヘックス文字に切り詰めてフィンガープリント化。注入前にフローの `results` リストに同じ `result_hash` が存在するか確認。存在する場合、呼び出しは no-op：再注入もリビジョン増分も行わない。これにより announce パイプラインの重複配信が安全になる——重複配信が状態を破損しない。

---

## 生成済み子エージェントを伴う競合リトライ

子セッションが既に spawn 済みだが、楽観的ロックの書き込みが競合に敗れた場合、子エージェントは決して黙って破棄されない。`_shared.py` の `update_flow_with_conflict_retry()` がこれを処理：

1. spawn 成功後、呼び出し側が `build_state` コールバックでこの関数を呼ぶ。
2. `FlowConflictError` 時：フローを再読みし、最新データに対して `build_state(fresh_flow, attempt)` を再呼び出し、最大 `PERSIST_MAX_ATTEMPTS = 3` 回までリトライ。
3. `FlowNotFoundError` または終端状態の新フロー時：spawn 済みの全 `child_session_key` を名指したエラーを返す——呼び出し側は key で回復すべき、再ディスパッチすべきではない。
4. リトライ回数枯渇時：同様に全 child key を名指したエラーを返す。

`build_state` コールバックは新フローと試行回数を受け取り、最新データに対して状態を再構築可能（例：現在のステップ数に基づき `step_id` を再割り当て）。

---

## 永続化と接続ライフサイクル

`store_sqlite.py` はサブエージェントレジストリの設計図を踏襲：

- **データベース**：`agent/tools/taskflow/data/taskflow_registry.db`
- **テーブル**：`task_flows(flow_id TEXT PK, state_json TEXT NOT NULL, wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL, child_session_key TEXT)`
- **WAL モード**：プロセス単位で `_switch_to_wal_if_needed()` により 1 回切り替え。ファイルが既に WAL の場合は pragma をスキップ。
- **ビジータイムアウト**：全接続で `5000ms`（最初のステートメント）。journal-mode 切替はこれを確実に尊重しないため、try/except で個別に許容。
- **非同期初期化**：プロセス単位 1 回、呼び出し元イベントループが所有する `asyncio.Lock` 配下で実行。他ループは `_initialized` をポーリング（他ループのロックに触れない——非スレッドセーフな `call_soon` ウェイクアップを回避）。
- **同期パス**：`get_flow_sync()` は標準ライブラリ `sqlite3` を使用し、`threading.Lock` で保護された 1 回限りのテーブル作成。失敗時はログ出力し `None` を返す（イベントループが存在しないシステムプロンプト注入用）。

---

## 既知の制限

- **`done` ≠ 成功**：ステップ `done` は「結果が注入された」ことのみを意味し、「子エージェントが成功した」ことを意味しない。`failed`/`skipped` ステップ状態は存在せず、`validation_criteria` も一致する `retry_policy` も持たないステップは、子が失敗しても `done` となり後続をアンロックする。失敗対応のリカバリにはステップに `retry_policy` を付与：`taskflow_wait_all` はリトライ予算が残る間、終了した子を再ディスパッチし、予算尽き後は失敗注記付きで `done` にマークする。`validation_criteria` がある場合、判定器の `block` 判定またはリトライ予算の枯渇が代わりにステップを `blocked` にする。
- **`taskflow_wait_all` タイムアウトは有界ポーリング**：完了しない子セッションが自動的にフローを失敗させることはない。タイムアウトは部分レポートを返す。
- **ステップ id は順次割当**：登録時に `step-{len(steps)+1}` を割当。ステップが並行して追加される場合、id は競合リトライ時に `build_state` 内で再計算。
