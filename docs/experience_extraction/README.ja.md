# 経験抽出アーキテクチャ

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

本書は、Agent が実行中に**いつ**経験を抽出し、**どの機構で**抽出し、経験が**どこへ**書き込まれるかを整理する。ライフサイクルには 5 本の抽出経路が組み込まれている：ターンごとの facts パイプライン、10 ターンごとの memory nudge、todo 全完了時の plan extraction、圧縮前の memory flush、圧縮後の todo fork。

> 以下の主張はすべてソースと照合済み。シンボル名、設定キー、既定値、パスはいずれも `agent/middlewares/`、`context_engine/facts/`、`agent/tools/`、`config/features/` のコードに実在する。

## 設計原則

1. **すべての抽出は既存ストアを拡張する。** 産出は MEMORY.md / USER.md、階層型 `facts/*.md`、plan 知識ディレクトリ、`skills/auto/`、`todos.db` のいずれかへ入る。並列ストアを作る抽出経路は一つもない。
2. **全面 fail-open。** どのトリガも自身の失敗をログに記録して握り潰す。todo ストアの破損、plan ファイルの読み取り不能、LLM 呼び出しの失敗、カーソルの破損が、メインの対話ターンを阻塞したり中断したりしない。
3. **ターン経路はゼロ阻塞。** facts パイプラインと圧縮後 todo fork は fire-and-forget のバックグラウンドタスク。memory nudge と plan extraction は独立した子 Agent として走る（非同期経路では `asyncio.gather` で永続化と並行）。
4. **機構は必要に応じて使い分ける。** ツール使用を要する作業だけが完全な `create_agent` fork を使う（memory nudge、plan extraction、todo fork）。純粋な抽出（ターンごとの facts、memory flush）は補助 LLM 呼び出し 1 回で済ませる。

## トリガ × 機構 × 書込先

| トリガ | 機構（fork agent か / 呼び出し形態） | 書込先 |
|---|---|---|
| 毎ターン終了時 | facts パイプライン（`context_engine/facts/`）：まずターンを enqueue し、補助 LLM 抽出器を走らせる。agent ではない。plan 抽出ターンではスキップされ、そのターン自身の pass が保留区間を吸収する。 | `TieredMemoryStore.add_fact` 経由で `facts/<category>.md` |
| 10 ターンごと（`nudge_memory_threshold`） | memory nudge（`_nudge_memory`）：`create_agent` の nudge agent を fork し、`_MEMORY_REVIEW_PROMPT` を使う | `memory` ツール経由で MEMORY.md / USER.md |
| todo リストが全完了（`completed` / `cancelled`） | plan extraction（`_nudge_plan_extraction`）：nudge agent を fork し、`_PLAN_EXTRACTION_PROMPT` を使う | ① 知識 JSON ② `skills/auto/` ③ `facts/*.md` |
| 圧縮前（cut が実際にメッセージを破棄） | memory flush（`run_memory_flush[_sync]`）：安価な LLM 呼び出し 1 回。agent ではない | `MemoryStore.append_entries` 経由で MEMORY.md と USER.md |
| 圧縮後（cut が実際にメッセージを破棄） | todo fork（`update_todos_from_compaction`）：fire-and-forget の nudge agent、`_COMPRESSION_TODO_PROMPT` | メインセッション束縛の `todowrite` シム経由で `todos.db` |

## トリガ詳細

### 1. ターンごとの facts パイプライン（session-memory P2-3）

`ContextEngineHook.aafter_agent`（`agent/middlewares/context_engine/core.py`）はまず最終ターンを MesMemory に永続化し、続いて `turn_num > 0` かつ当該ターンが plan 抽出ターン**ではない**とき、`_run_facts_pipeline(session_id, turn_num)` のバックグラウンドタスクを生成する。

`_run_facts_pipeline` は `enqueue_turn` の後に `process_pending` を呼ぶ（`context_engine/facts/queue.py`）：

- `enqueue_turn` は `enqueued` ウォーターマーク（`facts_cursor_enqueued`）を永続化済みターン番号まで進める。
- `process_pending` は 2 つのウォーターマーク間の保留区間を読み、会話行を整形し、補助 LLM で `extract_facts`（`context_engine/facts/extractor.py`）を呼ぶ。各 `{"category", "fact"}` は `TieredMemoryStore.add_fact` で書かれ、その後でのみ `consumed` ウォーターマーク（`facts_cursor_consumed`）を進める。

カーソルは**双ウォーターマーク**（`context_engine/facts/cursor.py`）：両値は単調で `state_register_db` に永続化されるため、enqueue と consume の間でクラッシュしても区間は失われず再生される。これは at-least-once 意味論であり、再生されたターンは重複 facts を生み得るが、`add_fact` が完全一致テキストで重複排除する。

**plan 抽出ターンの譲歩。** plan extraction が発火するとターンごとのパイプラインは起動しない。`_nudge_plan_extraction` が同じ LLM pass で保留区間を覆い（後述 Part 3）、成功後にのみ consumed ウォーターマークを進めるので、同じターンで 2 つの抽出器が走ることも区間が失われることもない。

### 2. 10 ターンごとの memory nudge

`ContextEngineHook._after_agent_impl` は毎ターン `state_register_db` の `nudge_review_memory_count` を増やす。カウンタが `nudge_memory_threshold`（既定 10）に達すると 0 に戻し、`nudge_review_memory_lock`（`state_register_mem`）の下で `_nudge_memory(session_id, system_prompt, messages)` を走らせる。いずれかの nudge ロックが保持されている間、`after_agent` は nudge 判断をスキップする（カウンタは増え続ける）。

`_nudge_memory`（`agent/middlewares/context_engine/nudge.py`）は `_create_nudge_agent` で nudge agent を構築し、会話に `_MEMORY_REVIEW_PROMPT` を `HumanMessage` として追加して呼び出す。プロンプトは、持続的なユーザー特性（ペルソナ、好み、個人的詳細）と振る舞いへの期待を `memory` ツールで保存するよう求め、保存対象がなければ "Nothing to save." と答えて停止させる。

- nudge agent はメイン LLM 上の独立した `create_agent` で、ミドルウェアは `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`、checkpointer なし。
- `_NudgeLimitTool`（`allowed_metadata_key` 未指定）は metadata に `nudge: True` を持つツールだけを通す。`memory`、`skill_list`、`skill_view`、`skill_manage`、`knowledge` がこのマークを持つため、nudge agent は memory を書けるが任意のメインツールは呼べない。
- fork のメッセージはログのみ。`res["messages"]` の内容はメイングラフへ一切入らない。

### 3. todo 全完了時の plan extraction

`_detect_todo_all_complete(session_id)`（`core.py`）は完了サイクルごとに 1 回発火する：

- todo が存在し、すべて `completed` または `cancelled` である；
- `nudge_plan_extraction_fired`（`state_register_db`）が未設定である。

遷移時にフラグを立て、リストが（もはや）全完了でなければ `False` に戻すため、次の全完了サイクルで再び発火する。読み取りは fail-open。

`plan_extraction_enabled` が有効で検出が発火すると、`_nudge_plan_extraction` が `nudge_plan_extraction_lock` の下で走る：

1. `_build_plan_context` が plan ファイル（`plan_ref` 状態を優先、次に `plan_ref` を持つ最初の todo）、todo リスト、start-work ledger（`.omo/start-work/ledger.jsonl`）、本セッションの子 Agent 実行記録（`result_text` は 24 KB に切り詰め、`outcome`、task）を集める。todo リストがなければ `{}` を返し、呼び出し側はそれを見てスキップする。
2. `_fetch_pending_facts` が未消費の facts 区間を Part 3 用に描画する。
3. plan コンテキストと facts 断片を差し込んだ `_PLAN_EXTRACTION_PROMPT` を、会話とともに nudge agent（同じビルダー、同じ `nudge: True` ゲート）へ送る。
4. `ainvoke` 成功後、保留 facts 区間が存在すれば `_advance_facts_consumed` が consumed ウォーターマークを進める。

プロンプトは 3 種類の産出を行う：

- **Part 1：構造化知識。** `knowledge(action="write", ...)` が JSON 文書を `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan-name>/`）へ書く：`task-<position>.json`、`wave-<index>.json`、`plan-summary.json`。各 task は `failure_set`、`success_path`、`method` を持ち、wave は失敗 / 成功パターン、plan は全体手法、主要な失敗 / 成功、再利用パターンを持つ。
- **Part 2：スキルライブラリ更新。** `skill_manage` がロード済みまたは既存のクラスレベルスキルを修正し、サポートファイルを追加し、または `skills/auto/` に新しいクラスレベル umbrella を作成する。プロンプトは明確に能動的で（"most completed plans produce at least one skill update"）、ユーザーの訂正、ワークフローの訂正、非自明な技法、陳腐化したスキルを一次信号として挙げる。
- **Part 3：持続 facts。** `memory(action="fact_add", target="<category>", content="<fact>")` が `facts/*.md` へ書く。このブロックは保留 facts 区間が存在するときだけ描画され、ユーザー好み、プロジェクト規約、主要な意思決定、ツール経験を抽出し、一時的な進捗は抽出しない。

後続の読み取りは同じツール（`knowledge(action="read")`）が提供し、圧縮された plan 要約は `build_knowledge_block`（`knowledge/prompt_block.py`）がシステムプロンプトへ自動注入する。

### 4. 圧縮前 memory flush（session-memory P0-1）

両方の圧縮経路（`agent/middlewares/summarization/core.py` の `_apply_compression_under_lock` と `_aapply_compression_under_lock`）は、cut がメッセージを破棄するとき、要約生成の前に flush を走らせる：

- 同期経路：`run_memory_flush_sync(...)`；
- 非同期経路：`await run_memory_flush(...)`。

`should_flush`（`agent/middlewares/summarization/memory_flush.py`）のゲートは `MEMORY_FLUSH["enabled"]` に加え、`total_chars >= force_flush_chars`（50 000）または `estimated_tokens >= soft_threshold_tokens`（8 000）。flush は破棄直前のテキストに対する `llm.ainvoke` / `llm.invoke` 1 回で、プロンプトは `_FLUSH_PROMPT`、`_build_llm` が `model=MEMORY_FLUSH["model"]`、`max_tokens=2048`、`timeout=30` で構築する。**agent ではなく**、ツールも持たない。

応答は `§` 区切りの条目として解析される。空結果または `(none)` は何も書かない。そうでなければ `MemoryStore.append_entries` が各条目を振り分ける：`^\s*user\s*:`（大文字小文字無視）に一致するものは USER.md、それ以外（Environment / Project / Decision / Tool / 接頭辞なし）は MEMORY.md。`append_entries` は対象ファイルに対して重複排除し、`add` と違い最古の条目を退避させて各ファイルの上限内に収める。

flush はクロスセッション facts だけを抽出する。一時的なタスク進捗は意図的に要約へ委ねる。例外はログに記録して握り潰し、flush が圧縮を阻塞することはない。

### 5. 圧縮後 todo fork

同じ圧縮点で、`_schedule_compression_todo_update`（`summarization/core.py` -> `nudge.schedule_compression_todo_update`）が `update_todos_from_compaction` を fire-and-forget の `asyncio.create_task` としてスケジュールする。スケジューラのゲートは**すべて**を満たす必要がある：

- `compression_todo_update_enabled`（`SUMMARIZATION`、既定 `True`）；
- cut が実際にメッセージを破棄した；
- 当該セッションで `compression_todo_update_lock` が保持されていない；
- セッションに空でない todo リストがある；
- 実行中のイベントループが存在する（同期圧縮経路はスキップし、debug ログを出す）。

`update_todos_from_compaction` は nudge agent を走らせ、システムプロンプトは `_COMPRESSION_TODO_PROMPT`、ツールはちょうど 1 つ：`_build_main_session_todowrite(session_id)`。fork は現在の todo リストを破棄スライスと突き合わせ、実際に完了した項目を `completed`、放棄または置換された項目を `cancelled`、証拠のある新規作業を `pending` として追加し、**完全な**リストを 1 回の `todowrite` 呼び出しで書き戻す（デルタではなく全置換）。変化がなければそのまま書き戻す。

fork の結果メッセージはログのみ。メイングラフやその checkpointer には何も届かず、派生セッションのミドルウェア状態は `finally` でクリアされる。

## 隔離と安全

| ガード | 保護対象 |
|---|---|
| nudge metadata 許可リスト（`metadata["nudge"]`、`_NudgeLimitTool` 既定規則） | memory nudge と plan extraction は nudge 段階用にタグ付けされたツールしか呼べない。それ以外のメインツールはエラー `ToolMessage` を返し、実行されない。 |
| 圧縮 metadata 許可リスト（`metadata["todo_update"] is True`、`_NudgeLimitTool(allowed_metadata_key="todo_update")`） | 圧縮 fork は該当 metadata マーク付き `todowrite` シムだけを許可する。 |
| 派生セッションキー `<id>::compression-todo` | 圧縮 fork の `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 状態キーがメインセッションと衝突しない。fork 実行中、メインセッションは `awrap_model_call` の最中だからである。`compression_todo_update_lock` だけが意図的にメインセッションへ書かれ、クロスパス再入コーディネータとして働く。 |
| メインセッション束縛の `todowrite` シム | fork グラフは派生キーで走るため、状態注入された実 `todowrite` は誤ったセッションを解決してしまう。シムは実ツールの `args_schema` と `description` を逐語的に再利用し（スキーマドリフトゼロ）、注入 `session_id` を捨て、構築時に捕捉したメインセッション id を束縛する。 |
| 読み取り専用 fork、checkpointer なし | 各 nudge / 抽出 fork の結果メッセージはログのみで破棄される。fork に checkpointer はなく、メイングラフ状態を書けない。 |
| セッション単位の再入ロック | `nudge_review_memory_lock`、`nudge_plan_extraction_lock`、`compression_todo_update_lock` が同一抽出経路の重複実行を防ぐ。nudge ロック保持中、`after_agent` は nudge 判断をスキップする。 |
| fail-open 境界 | 各経路は `try/except` で作業を包み、ログして返す。抽出失敗がターン、圧縮、他の抽出へ伝播しない。 |
| バックグラウンドタスクの参照保持 | `_BACKGROUND_TASKS` と `_COMPRESSION_TODO_TASKS` 集合が強参照を保持し、asyncio が実行中タスクを GC しないようにする。 |

## ストレージと上限

| ストア | パス（定数） | 上限 |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"`（`workspace/memory/MEMORY.md`） | 2200 文字（`MemoryStore.memory_char_limit`） |
| USER.md | `config.path.MEMORY_DIR / "USER.md"`（`workspace/memory/USER.md`） | 1375 文字（`MemoryStore.user_char_limit`） |
| facts 層 | `config.path.FACTS_DIR`（`workspace/memory/facts/<category>.md`） | 1 ファイル 4000 文字、5 カテゴリ：`environment`、`project`、`decisions`、`user_prefs`、`tool_lessons`。オンデマンド読みのみで、システムプロンプトには注入されない |
| plan 知識 | `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan>/`） | plan ごとに `task-<n>.json`、`wave-<n>.json`、`plan-summary.json`。フィールド上限はプロンプトで誘導（150 / 100 文字） |
| スキル | `config.path.AUTO_SKILLS_DIR`（`skills/auto/`） | `SKILL.md` と `references/`、`templates/`、`scripts/` サポートファイル |
| todos | `agent/tools/todolist/data/todos.db`（`store_sqlite._DB_PATH`） | セッションスコープのリスト、全置換書込 |

## 設定スイッチ

| キー | 場所 | 既定値 | 効果 |
|---|---|---|---|
| `MEMORY_FLUSH_ENABLED` | env -> `MEMORY_FLUSH["enabled"]`（`config/features/agent_side/memory_flush.py`） | `1`（オン） | 圧縮前 flush のマスタースイッチ |
| `MEMORY_FLUSH_MODEL` | env -> `MEMORY_FLUSH["model"]` | `""`（ファクトリ既定を使用） | flush に使う安価な抽出モデル |
| `soft_threshold_tokens` | `MEMORY_FLUSH` | `8000` | 破棄スライスがこの規模以上なら flush |
| `force_flush_chars` | `MEMORY_FLUSH` | `50000` | 文字数がこれを超えたら無条件 flush |
| `output_max_tokens` | `MEMORY_FLUSH` | `2048` | flush 呼び出しの出力上限 |
| `timeout_seconds` | `MEMORY_FLUSH` | `30` | flush 呼び出しのタイムアウト |
| `compression_todo_update_enabled` | `SUMMARIZATION`（`config/features/agent_side/summarization.py`） | `True` | 圧縮後 todo fork を有効化 |
| `plan_extraction_enabled` | `CONTEXT_ENGINE_HOOK`（`config/features/agent_side/context_engine_hook.py`） | `True` | todo 完了時の plan extraction を有効化 |
| `nudge_memory_threshold` | `CONTEXT_ENGINE_HOOK` | `10` | memory nudge の間隔ターン数 |
| `facts_char_limit` | `TIERED_MEMORY`（`config/features/agent_side/tiered_memory.py`） | `4000` | カテゴリ別 facts ファイル上限 |
| `facts_categories` | `TIERED_MEMORY` | `environment`、`project`、`decisions`、`user_prefs`、`tool_lessons` | 固定カテゴリ集合 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 実際の圧縮後の能動的圧縮クールダウン |
| `memory_char_limit` / `user_char_limit` | `MemoryStore.__init__` | `2200` / `1375` | MEMORY.md / USER.md の上限 |

## 検証

対象テスト：

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/context_engine/test_plan_extraction.py \
    tests/context_engine/facts/test_facts_extraction.py \
    tests/agent/tools/test_memory_store.py -q
```

- `test_compression_todo_update.py`：トリガゲート、fire-and-forget スケジュール、再入ロック、fail-open 解放、プロンプト内容、`todo_update` metadata ゲート、および完全 fork 隔離（派生キー、メインセッション `todowrite` シム、checkpointer / メッセージ漏洩なし）。
- `test_compression_cooldown_persist.py`：クールダウンの再起動間生存。
- `test_memory_flush.py`：flush ゲート、振り分け、非阻塞失敗。
- `test_plan_extraction.py`：`_detect_todo_all_complete` の 4 分岐、`after_agent` 5 要素契約、nudge 派遣、facts 譲歩、`_build_plan_context`。
- `test_facts_extraction.py` と `test_memory_store.py`：抽出器の解析 / 再生意味論と 2 つの memory ストア。

AI 判定評価：`evals/nudge_extraction/suite.py` が完了済み plan の実行（plan ファイル、完了 todos、合成子 Agent 実行記録、保留 facts ターン）を用意し、実 `_nudge_plan_extraction` を呼び、続いて補助 LLM judge に、生成されたスキルが今回の実行に真に根ざし、再利用可能で、汎用的でないかを判定させる。すべての書込はサンドボックスへリダイレクトされ、実 `skills/auto/` と `workspace/` は一切触れられない。

```bash
uv run python evals/evals.py nudge_extraction
```

スイートは `knowledge_written`、`facts_written`、`skills_created`、`ai_judge_skill_quality`、`no_repo_pollution` を検査する。

## 既知の境界

- **圧縮 fork には `todoread` を与えない。** `todo_update` マーク付き `todowrite` シムだけを許可する。fork は現在のリストをプロンプトで受け取るため、`todoread` は意図的に未タグのままにする（`agent/tools/todolist/tools/__init__.py`）。
- **memory flush はクロスセッション facts だけを抽出する。** 一時的なタスク進捗は要約に属し、MEMORY.md / USER.md には入らない。
- **facts 抽出は at-least-once。** 空 facts リストは有効な結果であり、それでも consumed ウォーターマークを進める。非リストの LLM 応答は例外を投げ、ウォーターマークは動かず区間が再生される。再生は重複 facts を生み得るが、`add_fact` が完全一致テキストで重複排除する。
- **クールダウンは能動的圧縮だけを抑制する。** `compaction_cooldown_rounds`（3）は実際の圧縮後に T1 / T2 / T3 圧縮を止める。T4 / T5 のプロバイダエラー回復は、クールダウン、ターンごとの試行上限、その他の反スラッシュゲートを構造上バイパスする。
- **plan extraction は facts 区間を吸収する。** plan 抽出ターンではターンごとのパイプラインをスキップし、Part 3 が同じ pass で保留区間を覆う。consumed ウォーターマークは、区間が実際に注入され、かつ pass が成功したときにのみ進む。

## 関連ドキュメント

- [セッションメモリ設計](../session_memory/README.md)：SESSION プランの P2-3（facts）と P0-1（memory flush）。
- [要約圧縮](../summarization/README.md)：圧縮トリガと、flush および todo fork をゲートするクールダウン。
- [長時間タスク](../long-running-tasks/README.md)：TaskFlow と、plan extraction が読み取る todo 計画層。
- [ミドルウェア README](../../agent/middlewares/README.md)：ContextEngineHook と Summarization のリファレンス。
