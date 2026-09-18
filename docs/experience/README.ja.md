# 経験システムアーキテクチャ

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

本書は経験システムを扱う：Agent が実行中に**いつ**経験を抽出し、**どの機構で**抽出し、経験が**どこへ**書き込まれ、そして生成されたスキルライブラリがどう維持されるか。ライフサイクルには 4 本の抽出経路が組み込まれている：圧縮時 memory review（`nudge_memory_threshold` 回の圧縮ごと）、圧縮時に todo 全完了だった場合の plan extraction、圧縮前の memory flush、圧縮後の todo fork。産出は 4 つのストア——MEMORY.md / USER.md、plan 知識ディレクトリ、`skills/auto/`、`todos.db`——へ入り、以下の **Curator** 節が plan extraction の書込先である `skills/auto/` を維持するバックグラウンドパスを記録する。

> 以下の主張はすべてソースと照合済み。シンボル名、設定キー、既定値、パスはいずれも `agent/middlewares/`、`agent/tools/`、`config/features/` のコードに実在する。

## 設計原則

1. **すべての抽出は既存ストアを拡張する。** 産出は MEMORY.md / USER.md、plan 知識ディレクトリ、`skills/auto/`、`todos.db` のいずれかへ入る。並列ストアを作る抽出経路は一つもない。
2. **全面 fail-open。** どのトリガも自身の失敗をログに記録して握り潰す。todo ストアの破損、plan ファイルの読み取り不能、LLM 呼び出しの失敗、カーソルの破損が、メインの対話ターンを阻塞したり中断したりしない。
3. **ターン経路はゼロ阻塞。** 圧縮後 todo fork と圧縮時 nudge はどちらも fire-and-forget のバックグラウンドタスク; memory review と plan extraction は NUDGE レーン上で独立した子 Agent として走り、モデル呼び出しを決してブロックしない。
4. **機構は必要に応じて使い分ける。** ツール使用を要する作業だけが完全な `create_agent` fork を使う（memory nudge、plan extraction、todo fork）。純粋な抽出（memory flush）は補助 LLM 呼び出し 1 回で済ませる。

## トリガ × 機構 × 書込先

| トリガ | 機構（fork agent か / 呼び出し形態） | 書込先 |
|---|---|---|
| `nudge_memory_threshold` 回の圧縮ごと（既定 10） | memory nudge（`_nudge_memory`）：`create_agent` の nudge agent を fork し、`_MEMORY_REVIEW_PROMPT` を使う | `memory` ツール経由で MEMORY.md / USER.md |
| 圧縮時に todo リストが全完了（`completed` / `cancelled`） | plan extraction（`_nudge_plan_extraction`）：nudge agent を fork し、`_PLAN_EXTRACTION_PROMPT` を使う | ① 知識 JSON ② `skills/auto/` |
| 圧縮前（cut が実際にメッセージを破棄） | memory flush（`run_memory_flush[_sync]`）：安価な LLM 呼び出し 1 回。agent ではない | `MemoryStore.append_entries` 経由で MEMORY.md と USER.md |
| 圧縮後（cut が実際にメッセージを破棄） | todo fork（`update_todos_from_compaction`）：fire-and-forget の nudge agent、`_COMPRESSION_TODO_PROMPT` | メインセッション束縛の `todowrite` シム経由で `todos.db` |

## トリガ詳細

### 1. 圧縮時の memory review

`schedule_compression_nudges`（`agent/middlewares/summarization/nudges.py`、Summarization ミドルウェアがメッセージを実際に破棄する compact ごとに呼び出す）は、圧縮ごとに `state_register_db` の `nudge_review_memory_count` を 1 回増やす。カウンタが `nudge_memory_threshold`（既定 10）に達すると 0 に戻し、`_nudge_memory(session_id, system_prompt, messages)` を fire-and-forget タスクとして派遣し、`nudge_review_memory_lock`（`state_register_mem`）の下で走らせる。いずれかの nudge ロックが保持されている間、圧縮はカウンタを増やすが派遣はしない。

`_nudge_memory`（`agent/middlewares/summarization/nudges.py`）は `_create_nudge_agent` で nudge agent を構築し、会話に `_MEMORY_REVIEW_PROMPT` を `HumanMessage` として追加して呼び出す。プロンプトは、持続的なユーザー特性（ペルソナ、好み、個人的詳細）と振る舞いへの期待を `memory` ツールで保存するよう求め、保存対象がなければ "Nothing to save." と答えて停止させる。

- nudge agent はメイン LLM 上の独立した `create_agent` で、ミドルウェアは `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`、checkpointer なし。
- `_NudgeLimitTool`（`allowed_metadata_key` 未指定）は metadata に `nudge: True` を持つツールだけを通す。`memory`、`skill_list`、`skill_view`、`skill_manage`、`knowledge` がこのマークを持つため、nudge agent は memory を書けるが任意のメインツールは呼べない。
- fork のメッセージはログのみ。`res["messages"]` の内容はメイングラフへ一切入らない。

### 2. 圧縮時の plan extraction（todo 完了）

`_detect_todo_all_complete(session_id)`（`summarization/nudges.py`）は圧縮ごとに評価され、完了サイクルごとに 1 回発火する：

- todo が存在し、すべて `completed` または `cancelled` である；
- `nudge_plan_extraction_fired`（`state_register_db`）が未設定である。

遷移時にフラグを立て、リストが全完了でなければ `False` に戻すため、次の全完了サイクルで再び発火する。読み取りは fail-open。検出は圧縮時にのみ走るため、一度も圧縮しないセッションは plan extraction を発火しない。

`plan_extraction_enabled` が有効で検出が発火すると、`_nudge_plan_extraction` が同じ接縫から fire-and-forget で派遣され、`nudge_plan_extraction_lock` の下で走る：

1. `_build_plan_context` が plan ファイル（`plan_ref` 状態を優先、次に `plan_ref` を持つ最初の todo）、todo リスト、start-work ledger（`.omo/start-work/ledger.jsonl`）、本セッションの子 Agent 実行記録（`result_text` は 24 KB に切り詰め、`outcome`、task）を集める。todo リストがなければ `{}` を返し、呼び出し側はそれを見てスキップする。
2. plan コンテキストを差し込んだ `_PLAN_EXTRACTION_PROMPT` を、会話とともに nudge agent（同じビルダー、同じ `nudge: True` ゲート）へ送る。

プロンプトは 3 種類の産出を行う：

- **Part 1：構造化知識。** `knowledge(action="write", ...)` が JSON 文書を `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan-name>/`）へ書く：`task-<position>.json`、`wave-<index>.json`、`plan-summary.json`。各 task は `failure_set`、`success_path`、`method` を持ち、wave は失敗 / 成功パターン、plan は全体手法、主要な失敗 / 成功、再利用パターンを持つ。
- **Part 2：スキルライブラリ更新。** `skill_manage` がロード済みまたは既存のクラスレベルスキルを修正し、サポートファイルを追加し、または `skills/auto/` に新しいクラスレベル umbrella を作成する。プロンプトは明確に能動的で（"most completed plans produce at least one skill update"）、ユーザーの訂正、ワークフローの訂正、非自明な技法、陳腐化したスキルを一次信号として挙げる。

後続の読み取りは同じツール（`knowledge(action="read")`）が提供し、圧縮された plan 要約は `build_knowledge_block`（`knowledge/prompt_block.py`）がシステムプロンプトへ自動注入する。

### 3. 圧縮前 memory flush

両方の圧縮経路（`agent/middlewares/summarization/core.py` の `_apply_compression_under_lock` と `_aapply_compression_under_lock`）は、cut がメッセージを破棄するとき、要約生成の前に flush を走らせる：

- 同期経路：`run_memory_flush_sync(...)`；
- 非同期経路：`await run_memory_flush(...)`。

`should_flush`（`agent/middlewares/summarization/memory_flush.py`）のゲートは `MEMORY_FLUSH["enabled"]` に加え、`total_chars >= force_flush_chars`（50 000）または `estimated_tokens >= soft_threshold_tokens`（8 000）。flush は破棄直前のテキストに対する `llm.ainvoke` / `llm.invoke` 1 回で、プロンプトは `_FLUSH_PROMPT`、`_build_llm` が `model=MEMORY_FLUSH["model"]`、`max_tokens=2048`、`timeout=30` で構築する。**agent ではなく**、ツールも持たない。

応答は `§` 区切りの条目として解析される。空結果または `(none)` は何も書かない。そうでなければ `MemoryStore.append_entries` が各条目を振り分ける：`^\s*user\s*:`（大文字小文字無視）に一致するものは USER.md、それ以外（Environment / Project / Decision / Tool / 接頭辞なし）は MEMORY.md。`append_entries` は対象ファイルに対して重複排除し、`add` と違い最古の条目を退避させて各ファイルの上限内に収める。

flush はクロスセッション facts だけを抽出する。一時的なタスク進捗は意図的に要約へ委ねる。例外はログに記録して握り潰し、flush が圧縮を阻塞することはない。

### 4. 圧縮後 todo fork

同じ圧縮点で、`_schedule_compression_todo_update`（`summarization/core.py` -> `nudges.schedule_compression_todo_update`）が `update_todos_from_compaction` を fire-and-forget の `asyncio.create_task` としてスケジュールする。スケジューラのゲートは**すべて**を満たす必要がある：

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
| セッション単位の再入ロック | `nudge_review_memory_lock`、`nudge_plan_extraction_lock`、`compression_todo_update_lock` が同一抽出経路の重複実行を防ぐ。nudge ロック保持中、圧縮スケジューラはその圧縮を数えるが派遣はしない。 |
| fail-open 境界 | 各経路は `try/except` で作業を包み、ログして返す。抽出失敗がターン、圧縮、他の抽出へ伝播しない。 |
| バックグラウンドタスクの参照保持 | `_COMPRESSION_TODO_TASKS` 集合が強参照を保持し、asyncio が実行中タスクを GC しないようにする。 |

## ストレージと上限

| ストア | パス（定数） | 上限 |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"`（`workspace/memory/MEMORY.md`） | 2200 文字（`MemoryStore.memory_char_limit`） |
| USER.md | `config.path.MEMORY_DIR / "USER.md"`（`workspace/memory/USER.md`） | 1375 文字（`MemoryStore.user_char_limit`） |
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
| `plan_extraction_enabled` | `NUDGE`（`config/features/agent_side/nudge.py`） | `True` | todo 完了時の plan extraction を有効化 |
| `nudge_memory_threshold` | `NUDGE` | `10` | memory review の間隔となる圧縮回数 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 実際の圧縮後の能動的圧縮クールダウン |
| `memory_char_limit` / `user_char_limit` | `MemoryStore.__init__` | `2200` / `1375` | MEMORY.md / USER.md の上限 |

## 検証

対象テスト：

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_nudges.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/system_prompt/test_plan_extraction.py \
    tests/agent/tools/test_memory_store.py -q
```

- `test_compression_todo_update.py`：トリガゲート、fire-and-forget スケジュール、再入ロック、fail-open 解放、プロンプト内容、`todo_update` metadata ゲート、および完全 fork 隔離（派生キー、メインセッション `todowrite` シム、checkpointer / メッセージ漏洩なし）。
- `test_compression_nudges.py`：圧縮時 nudge ディスパッチ（memory review + plan extraction が compact 接縫から発火；カットなし圧縮は何もディスパッチしない）。永続化アサーションは `tests/agent/middlewares/message_persistence/` にあります。
- `test_compression_cooldown_persist.py`：クールダウンの再起動間生存。
- `test_memory_flush.py`：flush ゲート、振り分け、非阻塞失敗。
- `test_plan_extraction.py`：`_detect_todo_all_complete` の 4 分岐、`schedule_compression_nudges` の圧縮時カウンタ / ロック意味論、派遣、`_build_plan_context`。
- `test_memory_store.py`：MEMORY.md / USER.md ストアの意味論。

AI 判定評価：`evals/nudge_extraction/suite.py` が完了済み plan の実行（plan ファイル、完了 todos、合成子 Agent 実行記録）を用意し、実 `_nudge_plan_extraction` を呼び、続いて補助 LLM judge に、生成されたスキルが今回の実行に真に根ざし、再利用可能で、汎用的でないかを判定させる。すべての書込はサンドボックスへリダイレクトされ、実 `skills/auto/` と `workspace/` は一切触れられない。

```bash
uv run python evals/evals.py nudge_extraction
```

スイートは `knowledge_written`、`skills_created`、`ai_judge_skill_quality`、`no_repo_pollution` を検査する。

## 既知の境界

- **圧縮 fork には `todoread` を与えない。** `todo_update` マーク付き `todowrite` シムだけを許可する。fork は現在のリストをプロンプトで受け取るため、`todoread` は意図的に未タグのままにする（`agent/tools/todolist/tools/__init__.py`）。
- **memory flush はクロスセッション facts だけを抽出する。** 一時的なタスク進捗は要約に属し、MEMORY.md / USER.md には入らない。
- **クールダウンは能動的圧縮だけを抑制する。** `compaction_cooldown_rounds`（3）は実際の圧縮後に T1 / T2 / T3 圧縮を止める。T4 / T5 のプロバイダエラー回復は、クールダウン、ターンごとの試行上限、その他の反スラッシュゲートを構造上バイパスする。

## 関連ドキュメント

- [セッションメモリ設計](../session_memory/README.md)：圧縮前 memory flush。
- [要約圧縮](../summarization/README.md)：圧縮トリガと、flush および todo fork をゲートするクールダウン。
- [長時間タスク](../long-running-tasks/README.md)：TaskFlow と、plan extraction が読み取る todo 計画層。
- [ミドルウェア README](../../agent/middlewares/README.md)：`@dynamic_prompt` システムプロンプト注入と Summarization のリファレンス。
