# 🛠️ WorkMode サブメカニズム — タスクレベルの信頼性と調整

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> [長時間実行タスク](../README.ja.md)の一部：実行中の TaskFlow を、中断・サイレント停止・低品質ステップ・繰り返し計画・タスク事実の欠落から守る、五つの任意メカニズム。WorkMode 式長タスク体系の**サブメカニズム**を借用し、三層組織は複製しません。エキスパートパネルモードが「誰が行うか」を答えるのに対し、これらは「どう信頼して行うか」を答えます。

**コードの真実源：** `agent/middlewares/task_intent_router/`、`agent/tools/taskflow/watchdog.py`、`agent/tools/taskflow/step_judge.py`、`agent/tools/taskflow/template_store.py`、`agent/tools/taskflow/tools/`、`agent/tools/subagent/spawn/system_prompt.py`、`config/features/agent_side/taskflow_infra.py`、`config/path.py`。以下の定数とコードパスはすべて照合済みです。

## 五つのメカニズム一覧

| メカニズム | 解決する問題 | 所在 | 既定値 |
| :--- | :--- | :--- | :--- |
| 意図分類による入力ルーティング | 実行中のユーザーメッセージがタスクを盲目に中断する | `agent/middlewares/task_intent_router/` の before-agent ミドルウェア | アクティブな flow のみ；fail-open |
| ウォッチドッグとデッドロック検出 | 停止・空転する flow が誰にも気づかれない | `agent/tools/taskflow/watchdog.py`、サブエージェント sweeper が走査 | 警告のみ、flow を変更しない |
| 採点による自己評価リドゥ | 二値 judge に品質勾配と再試行上限がない | `step_judge.py::judge_step_self_eval` ＋ resume 側 | ステップ単位で opt-in（`self_evaluate`） |
| 計画テンプレート再利用 | 同じ分解を毎回ゼロから作り直す | `agent/tools/taskflow/template_store.py`、`taskflow_flow` アクション | opt-in アクション |
| plan/environment 二ファイル | タスク事実（パス／スタック／制約／資格情報参照）が会話に散在 | flow `state["environment"]` → 子のシステムプロンプト | 空ならプロンプト不変 |

五つとも**追加的**です。アクティブな flow・`self_evaluate`・テンプレートアクション・`environment` がなければ、動作は以前とバイト単位で同一です。

---

## 1. 意図分類による入力ルーティング

TaskFlow の実行中、新しいユーザーメッセージはまず `agent/middlewares/task_intent_router/intent.py` の `classify_turn_intent()` で分類されます。**rules-only** で LLM は呼ばず、既存 TaskIntent の四言語キーワード照合を踏襲します（中国語・英語・日本語・韓国語のキャンセル語を認識）。判定できない入力は `CONTINUE` に解決されます（fail-open：メッセージを落とさず、誤ルーティングしません）。

優先順位：`CANCEL` > `SOFT_PREEMPT` > `RESUME` > `CONTINUE` > `NEW`。

| 意図 | トリガー | ルート |
| :--- | :--- | :--- |
| `CANCEL` | 明確な停止語（`停下`、`cancel`、`やめて`、`중단`） | `taskflow_flow(action="cancel")` ですべてのアクティブ flow をキャンセルし、短い通知を注入 |
| `SOFT_PREEMPT` | 「待って／方向を調整」系の表現 | 次の turn に advisory steering を注入；実行中のステップはキャンセルも中断もしない |
| `RESUME` / `CONTINUE` | HITL の回答またはタスク補足 | 空操作——既存の turn／HITL 経路が処理 |
| `NEW` | 明らかに無関係な新トピック | advisory steering；サーバー側のキュー入力が権威あるキューのまま |

`TaskIntentRouterMiddleware` は `agent/middlewares/specs.py` で main 位置 30 に登録され（`ORDER IS A CONTRACT`）、任意の `DelegationPressureGate` の直前に置かれます。**アクティブな flow がなければ空操作**なので、短いタスクはこの経路に入りません。agent 層は server のキューを import できないため、SoftPreemption と New はサーバー側キューを直接操作せず、turn への advisory steering 注入で実現します。内部／システム指示メッセージは再分類されません（キャンセル通知が二度目のキャンセルを引き起こしません）。

---

## 2. ウォッチドッグとデッドロック検出

`agent/tools/taskflow/watchdog.py` は、既存の呼び出し級ガード（InvalidToolCallRepair、LLMRetry、MaxTokensBoost）にタスク級の生存性検出を追加します。サブエージェント registry の sweeper が liveness の後に `sweep_and_notify()` を呼びます。検出は純粋な **fail-open** で、flow 状態を一切書き換えず、`system.notice` 警告（`source="taskflow:watchdog"`）を送るだけです。キャンセル・再試行・継続の判断は agent またはユーザーが行います。

`WatchdogAlert(flow_id, session_id, kind, detail)` の kind：

- **ハートビート期限切れ**（`heartbeat_timeout_s`、既定 600 秒）：flow が非終態で `updated_at` がしきい値より古い。
- **無進捗**（`deadlock_no_progress_s`、既定 1800 秒）：flow が長時間 running でステップ状態が変わらない。
- **ツールループ**（`deadlock_tool_fail_count`、既定 3）：同じ step／ツールが同じ引数で繰り返し失敗。
- **judge ループ**（`deadlock_judge_fail_count`、既定 5）：step の judge が繰り返し retry を返す。

しきい値はすべて `config/features/agent_side/taskflow_infra.py` にあります。`0` に設定するとその検査を無効化します。通知失敗は飲み込まれ、通知経路の故障が sweeper を止めません。

---

## 3. 採点による自己評価リドゥ

`StepJudge` の既定経路は不変です：二値の `PASS` / `RETRY` / `BLOCK` / `UNKNOWN` 裁定で、`UNKNOWN`（judge 不能使用）は fail-closed です。その上に、`step_judge.py::judge_step_self_eval` が `self_evaluate: true` のステップに**任意の採点経路**を追加します：

- 厳格な reviewer LLM が step の `judge_criteria` / `validation_criteria` に対して 1〜10 で採点し、`{"score", "reason", "feedback"}` を返します。
- スコア ≥ `self_eval_min_score`（既定 7）→ step は `DONE`。
- スコアが閾値未満で再試行予算（`self_eval_max_retries`、既定 5）が残る → `RETRY` し、スコアとフィードバックを step に追記。
- 予算切れ → `forced_pass: true` として step を `DONE` にし、主観的採点が無限ループしないようにします。
- judge 不能使用は `score=None` を返し、設定された `on_judge_error` 戦略に従います——黙って通過させることはありません。

三つの step フィールド（`self_evaluate`、`self_eval_min_score`、`self_eval_max_retries`）は `run_task` の `TaskflowStepInput` スキーマと `_OPTIONAL_STEP_FIELDS` ホワイトリストにあります。`self_evaluate` のない step は元の二値 judge 経路をそのまま通ります。

---

## 4. 計画テンプレート再利用

`agent/tools/taskflow/template_store.py` は完了した flow の**分解構造**（step id、タスク、依存、機能ロール、judge／validation 基準、response schema）を `APP_HOME/taskflow_templates/<name>.json`（`config.path.TASKFLOW_TEMPLATES_DIR`）に保存します。実行時フィールド（子セッション key、結果、revision、タイムスタンプ）はホワイトリストで除去されるため、テンプレートは再実行待ちの空殻であり、過去の実行のリプレイではありません。

二つの `taskflow_flow` アクションが公開します：

- `save_as_template(flow_id, name, description="")` —— flow の step 構造を原子書き込み（ファイルロック＋`tempfile`＋`os.replace`）。
- `load_from_template(name, flow_id, description="")` —— テンプレートの step 構造で新しい flow を作り、todo ルートを束縛。

これは PTC/LDW replay と明確に**相補**です：テンプレートは「作業をどう分解したか」を再利用し、PTC/LDW は「正確な実行呼び出し列」を再利用します。一つのタスクがテンプレートを load し、その後プリコンパイル済みスクリプトで呼び出しをリプレイできます。

> 段階的な step 展開は**採用しません**。Sherry のネストした TaskFlow が既にカバーしています：トップ level の flow は粗い step を持ち、step の子 ORCHESTRATOR が独自のサブ flow を作ります。進捗は `progress_push` / `system.notice` で上に伝わります。DAG 状態機に `expandable → expanded` 遷移を加えても（step は会話ではなく SQLite にあるため）圧縮の利点はなく、部分展開の finish ゲートという負担だけが増えます。

---

## 5. plan/environment 二ファイル

flow 作成時に `taskflow_flow(action="create", environment={...})` を指定できます。辞書は `state["environment"]` にそのまま保存され（`taskflow_create.py`）、資格情報は参照（`keychain://...`）のまま解析されません。`store_sqlite.get_flow_environment_sync(flow_id)` がグローバルに一意な flow id で読み返します。

step 派発時、`_dispatch.dispatch_child()` は明示渡された `environment` を優先し、なければ所属 flow の environment を読み、`spawn_subagent_direct()` に渡します。`SpawnRequest.environment` が `SubagentPromptContext` へ運び、`agent/tools/subagent/spawn/system_prompt.py` の `_render_environment_section()` がロール指示の**後**、ルールの**前**に `## Task Environment` ブロックを描画します。文字列以外は JSON 描画され、いずれかの値が `credential_ref` を含む場合、これは承認済みの資格情報ツールで解決する参照であり直接使用できる秘密ではないと子に注意します。

保証：

- environment が空または欠落 → セクションは**まったくなし**、既存の子プロンプトはバイト単位で不変。
- environment 読み取り失敗 → 派発は続行（fail-open）、子に environment ブロックが付くだけです。
- 派発時の明示 `environment` が flow 級の値より優先。
- environment はタスク級で、セッション級の `project_dir` / `AGENTS.md` 注入とは直交します。

---

## 設定リファレンス

| 設定（`TASKFLOW_INFRA`） | 既定 | 目的 |
| :--- | :--- | :--- |
| `heartbeat_timeout_s` | `600.0` | ハートビート期限切れ警告のしきい値 |
| `deadlock_tool_fail_count` | `3` | 同一ツール連続失敗回数 |
| `deadlock_judge_fail_count` | `5` | judge 連続 retry 回数 |
| `deadlock_no_progress_s` | `1800.0` | 無進捗警告のしきい値 |

## テスト

- `tests/agent/middlewares/test_task_intent_router.py` —— 四言語分類、アクティブ flow 空操作、キャンセル経路、内部メッセージ処理。
- `tests/agent/tools/taskflow/test_watchdog.py` —— 各種警告、しきい値、終態フィルタ、通知 fail-open。
- `tests/agent/tools/taskflow/test_self_eval.py` —— 採点解析、通過／リドゥ／強制通過、不能使用戦略、既定 judge への非影響。
- `tests/agent/tools/taskflow/test_template_store.py` —— 構造ホワイトリスト、原子 CRUD、merged アクション。
- `tests/agent/tools/taskflow/test_environment.py` —— 永続化、同期読み取り、派発注入／優先度／fail-open。
- `tests/agent/tools/subagent/spawn/test_system_prompt_environment.py` —— ブロック描画、資格情報参照、空値の後方互換。

`uv run pytest tests/agent/tools/taskflow -q` で実行します（CI ゲートはプロセス分離ランナー `tests/run_tests_split.py`）。
