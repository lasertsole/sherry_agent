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

| 状態          | 意味                                                                             | 遷移トリガー                                                   |
| ------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `blocked`     | 依存関係がまだすべて `done` ではない；`taskflow_run_task` は登録のみ、spawn なし | `taskflow_resume` が依存関係満了時にアンロック                 |
| `ready`       | 依存関係満た済み、ディスパッチ待ち                                               | `taskflow_dispatch` がディスパッチ                             |
| `dispatched`  | 分離された子セッションが spawn 済み、`child_session_key` 永続化済み              | `taskflow_resume` が結果注入                                   |
| `done`        | 結果が `taskflow_resume` で注入され、**かつ設定された品質ゲートをすべて通過**    | （このステップの終端状態）                                     |
| `failed`      | 確定的な失敗：呼び出し側が `step_outcome="failure"` を宣言、または `response_schema` の結果が再試行予算を使い切っても不適合 | （このステップの終端状態。依存側は `blocked` のまま）          |
| `skipped`     | `step_outcome="skipped"` の宣言、または依存がスキップ/キャンセルされた（スキップは下流へ連鎖） | （このステップの終端状態）                                     |
| `cancelled`   | ステップ完了前に `taskflow_cancel` がフローをキャンセル                          | （このステップの終端状態）                                     |

> `done` は「結果が注入され、そのステップに設定された品質ゲートをすべて通過した」ことを意味します。**期待が何も設定されていない**場合（`response_schema`、`judge_criteria`/`validation_criteria`、`retry_policy`）は従来どおり「結果が注入された」に退化するため、呼び出し側が `step_outcome="failure"` を宣言しない限り、失敗した子でも `done` になり得ます。
>
> `failed`・`skipped`・`cancelled` はいずれも依存側をアンロックしません。`failed` の依存は依存側を `blocked` のまま残し（失敗を黙って飛ばしてはいけない）、`skipped`/`cancelled` の依存は `skipped` を依存側へ連鎖させ、死んだ枝をその場で決着させてフローを永遠に待たせません。

### 期待 → 実績の閉ループ

ステップは**期待**を持てるようになり、resume 経路が**実績**を対にして記録するため、「このステップは要求どおりにやったか」がフロー状態だけで答えられます。

期待フィールド（すべて任意、`taskflow_run_task` が書き込み、ステップに保存）：

| フィールド            | 意味                                                                                     |
| --------------------- | ---------------------------------------------------------------------------------------- |
| `response_schema`     | 構造化結果が満たすべき JSON Schema（Tier 1）。spawn 層の `output_schema` として子エージェントの出力契約にもなります。 |
| `judge_criteria`      | LLM ステップ判定器に渡す自然言語の合格基準（Tier 2）。`judge_model` で判定器のモデルを上書きできます。 |
| `expected_params`     | そのステップが扱うはずの構造化入力（記録のみで、強制はしません）。                        |
| `input_bindings`      | `{"param": "step-A.structured_result.files"}`——上流の構造化結果を `## Input parameters` ブロックとして、このステップのタスク本文に追記します。 |
| `validation_criteria` | 旧来のテキスト基準。存在すれば判定されます（`judge_criteria` が無い場合に使用）。        |
| `retry_policy`        | 既存の失敗再試行ポリシー（`max_retries`、`retry_delay_seconds`、`retry_on`）。            |

実績フィールド（`taskflow_resume` が結果エントリに書き込み）：

| フィールド          | 意味                                                                          |
| ------------------- | ----------------------------------------------------------------------------- |
| `step_id`           | 対にするためのキー：この結果はどのステップのものか。                          |
| `structured_result` | 解析された構造化値（呼び出し側の `structured_result` または `result` 由来）。 |
| `schema_validated`  | `true` / `false` / `null`（schema 未設定）——Tier 1 の判定結果。               |
| `step_outcome`      | 呼び出し側が宣言した場合に記録（`success` / `failure` / `partial` / `skipped`）。 |

2 層の品質ゲートはこの順に連結され、どちらもステップ単位でオプトインです（実行順序は下の `taskflow_resume` を参照）。

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
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    aggregate_deps: bool = False,
    response_schema: dict | None = None,
    expected_params: dict | None = None,
    input_bindings: dict | None = None,
    judge_criteria: str | None = None,
    judge_model: str | None = None,
    functional_role: str | None = None,
    step_model: str | None = None,
    step_timeout_seconds: float | None = None,
    priority: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

フローにステップを登録し、分離された子エージェントへディスパッチします。ステップ id は `step-1`、`step-2` の順に自動採番されます。

- **依存なし**（または満た済み）→ 直ちに `dispatched`（子を spawn）。
- **依存未満** → `blocked` として登録し spawn しません。未知の依存 id は spawn や状態変更の前に拒否されます。
- **期待パラメータ** はステップに保存され resume で効きます：`response_schema`（spawn 層には `output_schema` として渡されます）、`judge_criteria` / `judge_model`、`expected_params`、`input_bindings`。
- **実行メタ情報** は子へ引き継がれます：`functional_role`（researcher / executor / reviewer / librarian / general）、`step_model`、`step_timeout_seconds`。`priority` はディスパッチ順のヒントです。
- 指定しなかったフィールドはステップに書かれません。これが「期待を持たないフローは閉ループ導入前とバイト単位で同じ」である理由です。
- `step_id`、`child_session_key`、`revision` を返します（ブロック時は `pending=[...]` で未充足の依存を列挙）。
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
    structured_result: dict | None = None,
    step_outcome: str | None = None,
) -> str
```

完了した子セッションの結果をフロー状態に注入します（冪等）。results に `{child_session_key, result, result_hash, injected_at, step_id, schema_validated}` を追記します（判明していれば `structured_result` / `step_outcome` も）。フローが `waiting` なら `running` に戻します。

**実行順序**（各ゲートはステップ単位のオプトインで、連結されます）：

1. **失敗時の再試行** —— ステップに `retry_policy` があり結果が失敗なら、決着させずに代替の子で再ディスパッチします。失敗シグナルは分類された結果テキストと `schema_validated=false` の両方です。宣言した schema に構造が合わないのはテキスト分類では見えない失敗で、ここで再ディスパッチされます——**判定器呼び出しより前**なので、壊れた構造が判定トークンを消費することはありません。
2. **Tier 1 —— 構造。** ステップに `response_schema` があると、結果（呼び出し側の `structured_result`、無ければ `result` を JSON として解析）を照合し、判定を結果エントリに記録します。再試行予算を使い切った失敗はステップを `failed` にし、`fail_reason` に schema エラーを残します。通過した場合は Tier 2 へ進みます——構造が正しいことは内容が正しいことを意味しません。
3. **Tier 2 —— 意味論。** ステップに `judge_criteria`（または旧来の `validation_criteria`）があると、auxiliary-LLM 判定器（`agent/tools/taskflow/step_judge.py`、温度 0）が結果（検証済みの `structured_result` があればそれ、無ければ結果テキスト）を評し、`pass` / `retry` / `block` を返します。`retry` は共有 `_retry` の継ぎ目で再ディスパッチし、ステップ自身の `retry_count` 予算（`STEP_JUDGE["max_retries"]`、既定 2）を再利用して `with_judge_feedback` で判定器の助言を代替タスクに付けます。予算切れまたは `block` 判定は理由付きでステップを `blocked` にします。判定器はフローの証跡サマリを受け取り、fail-open です——モデルエラーや解析不能な応答は `pass` に退行します。
4. **呼び出し側の宣言結果。** `step_outcome` は 2 つのゲートより優先されます：`failure` は判定器を呼ばずに `failed`、`skipped` は `skipped`、`success` / `partial` は記録のみでゲートは通常どおり走ります。不正な値は状態変更の前に拒否されます。

**DAG の記帳**：ゲート通過時は該当ステップを `done` にして `unlock_dependents()` を呼び、依存が満たされた `blocked` ステップを `ready` に移します。`failed` のステップは依存側を `blocked` のまま残し、`skipped`/`cancelled` のステップは `skipped` を依存側へ連鎖させます。アンロックされたステップ id を返します。**resume は新たに ready になったステップをディスパッチしません**——呼び出し側が `taskflow_dispatch` で明示的に行います。
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

フロー状態全体を読み取り専用で再読します：status、revision、child_session_key、description、全ステップ（status、depends_on、child_session_key に加え、設定された期待——`response_schema` の properties/required、`judge_criteria`、`judge_model`、`input_bindings`、`expected_params`、実行メタ情報、および `block_reason` / `fail_reason` / `skip_reason`）、ステータス別件数、results（各エントリの `step_id`、`schema_validated`、`structured_result`、`step_outcome`、`judge_verdict` / `judge_reason`、記録があれば `token_usage`）、wait ペイロード、summary、failure_reason、cancel_reason。リビジョン競合後の指定再読ステップでもあります。
### taskflow_progress

```python
async def taskflow_progress(flow_id: str) -> str
```

読み取り専用の完了レポート：完了率、ステータス内訳の全体（`done` / `dispatched` / `ready` / `blocked` / `failed` / `skipped` / `cancelled`）、着手すべき次のステップ、そして **Needs a decision** リスト——`failed` / `blocked` / `skipped` / `cancelled` の各ステップを記録された理由とともに列挙します。`done` ステップが 2 つ以上 `dispatched_at` を持つ場合は残り時間の推定も出します。フローは決して変更しません。
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

終端遷移。`finish` は `summary`、`fail` は `failure_reason`、`cancel` は `cancel_reason` を記録します。ディスパッチ済みの子セッションは動き続け、フローがキャンセルされるまで `taskflow_resume` で結果を届けられます。

`finish` は DONE への遷移を 4 つのチェックで順に守ります：**A** すべてのステップが `done` か `blocked`；**B** 未解決のステップを残さない——`failed`・`skipped`・`cancelled` のステップは id と記録された理由付きで報告され（呼び出し側の判断が必要です：resume・retry・cancel）、`blocked` も同様です；**C** フローの証跡（`agent/tools/taskflow/evidence_collector.py`）に `FAIL` も `[stale]` 行もない；**D** `SisyphusVerifier` が通る——呼び出し側が `todo` と `plan_path` の両方を渡したときだけ実行されるため、flow/step schema の移行は不要です。各ゲートは fail-open で、証跡コレクタが使えない場合や検証器のエラーは完了を妨げず通します。

`cancel` は終端化の前に、未完了のすべてのステップを `cancelled`（キャンセル理由付き）にします。フロー終了後もステップ状態が `ready` / `dispatched` / `blocked` を主張し続けないためです。
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

`steps` を**1 回だけ**走査し、依存が満たされた `blocked` ステップを `ready` に移し、今回アンロックしたステップ id を返します。単一パスなので依存サイクルが無限ループになることはありません。アンロックは**失敗を認識**します：`failed` で終わった（あるいはまだ `blocked` / 実行中の）依存は依存側を `blocked` のままにし、`skipped` または `cancelled` の依存は `skip_reason` とともに `skipped` を依存側へ連鎖させます——死んだ枝はその場で決着し、フローが永遠に待つことはありません。
---

### 合成——`aggregate_deps`

`taskflow_run_task` に `aggregate_deps=true` を渡すと、合成ステップは依存ステップの記録済み結果をディスパッチされるタスク本文へ受け取ります。集約は `agent/tools/taskflow/tools/_shared.py::build_task_with_dep_results(step, steps, results)` が構築します。`depends_on` 順に各依存ステップの `child_session_key` を flow の `{child_session_key, result, result_hash}` 台帳に照合し、`## Upstream Results` 見出しの下へ追記します。記録済み結果のない依存は `no result recorded` プレースホルダになります。

フラグはステップに保存されるため、`taskflow_dispatch`、StepJudge リトライ、再試行ポリシー経路はいずれも保存済み `task` を書き換えずに集約を再導出します。既定（`aggregate_deps` が無い、または false）ではディスパッチ本文は変更されません。

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

- **`done` ≠ 成功**：ステップ `done` は「結果が注入され、**かつそのステップに設定された品質ゲートをすべて通過した**」ことを意味します。**期待が何も設定されていない**場合（`response_schema`、`judge_criteria`/`validation_criteria`、`retry_policy`）は従来の意味に退化するため、呼び出し側が `step_outcome="failure"` を宣言しない限り、失敗した子は `done` になります。閉ループに失敗を捕まえさせるには、期待を宣言する（`response_schema` の検証は判定器呼び出しより前で無料です）か結果を宣言します。`failed` は依存側をブロックし、`retry_policy` は予算が残る限り終了した子を再ディスパッチします（判定器は `block` 判定や予算枯渇で `blocked` を付けます）。
- **`taskflow_wait_all` タイムアウトは有界ポーリング**：完了しない子セッションが自動的にフローを失敗させることはない。タイムアウトは部分レポートを返す。
- **ステップ id は順次割当**：登録時に `step-{len(steps)+1}` を割当。ステップが並行して追加される場合、id は競合リトライ時に `build_state` 内で再計算。
