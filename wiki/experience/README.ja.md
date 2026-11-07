# 経験システムアーキテクチャ

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

本書は経験システムを扱う：Agent が実行中に**いつ**経験を抽出し、**どの機構で**抽出し、経験が**どこへ**書き込まれ、そして生成されたスキルライブラリがどう維持されるか。ライフサイクルには 4 本の抽出経路が組み込まれている：圧縮時 memory review、プランがターン境界で完了したときの **スキルレビュー**、圧縮後の todo fork、そしてメインセッション削除時の session-end salvage。産出は 4 つのストア——MEMORY.md / USER.md / FACTS.md、`skills/auto/`、`todos.db`——と、監査用の生 `handoff-tree.json` に入る。以下の **Curator** 節は、スキルレビュー の書込先である `skills/auto/` を維持するバックグラウンドパスを記録する。

> **ストレージ更新（2026-11-04）：** 以下で MEMORY.md / USER.md / FACTS.md と呼んでいる 3 つのメモリ出力は、MesMemory の `memory_entries` テーブルの行（ターゲット `memory` / `user` / `facts` ごと）になりました。旧 Markdown ファイルは起動時に `memory_migration.py` が一度だけインポートし、`*.md.bak` に改名します。読み込みは `MemoryStore.load_from_store()` で、各行は `origin_session`（由来）と `usage_count` / `last_used`（使用状況）を持ちます。

> 以下の主張はすべてソースと照合済み。シンボル名、設定キー、既定値、パスはいずれも `agent/middlewares/`、`agent/tools/`、`config/features/` のコードに実在する。

## 設計原則

1. **すべての抽出は既存ストアを拡張する。** 産出は MEMORY.md / USER.md / FACTS.md、`skills/auto/`、`todos.db` のいずれかへ入る。生 handoff tree は監査用で、モデル向け知識ストアではない。並列ストアを作る抽出経路はない。
2. **全面 fail-open。** どのトリガも自身の失敗をログに記録して握り潰す。todo ストアの破損、plan ファイルの読み取り不能、LLM 呼び出しの失敗が、メインの対話ターンを阻塞したり中断したりしない。
3. **ターン経路はゼロ阻塞。** 圧縮後 todo fork と圧縮時バックグラウンドレビュー はどちらも fire-and-forget のバックグラウンドタスク。memory review と スキルレビュー は BACKGROUND_REVIEW レーン上で独立した子 Agent として走り、モデル呼び出しを決してブロックしない。
4. **すべての抽出経路がツールを必要とする。** memory review、スキルレビュー、todo fork はいずれも `create_agent` fork で、それぞれ `memory`、`skill_manage`、`todowrite` を通じて書き込む。独立した純粋抽出呼び出しは残っていない。
5. **プラン完了はスキルだけを保守する。** 完了パスは knowledge も FACTS も書かない。永続産出はクラスレベル skill の変更、または judge 承認済み no-op だけ。

## トリガ × 機構 × 書込先

| トリガ | 機構（fork agent か / 呼び出し形態） | 書込先 |
|---|---|---|
| 圧縮のたび | memory review（`_execute_memory_review`）：**破棄される**スライスだけを**原文のまま**、そのターンが使ったモデル構成（`_build_session_model`）で送り、セッションのシステムプロンプトとツール面も再現してプレフィックスキャッシュに命中させる；`BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"` なら折り畳む（直近 24 件は原文、それ以前は 1 行に切詰め） | `memory` ツール経由で MEMORY.md / USER.md / FACTS.md |
| プラン完了——ターン境界で全 todo が `completed` / `cancelled`（**圧縮時ではない**） | スキルレビュー（`_run_skill_review`）：完了フック（`PlanContinuationEnforcer` → `maybe_trigger_skill_review`）が**補助モデル**で専用 agent を fork し、`_SKILL_REVIEW_PROMPT` を使い、**会話履歴を一切渡さない** | `skills/auto/`（クラスレベルスキル + `<module>-notes`）。judge 承認済み no-op も可。生 `handoff-tree.json` は先に監査用として書込 |
| 圧縮後（cut が実際にメッセージを破棄） | todo fork（`update_todos_from_compaction`）：fire-and-forget の バックグラウンドレビュー agent、`_COMPRESSION_TODO_PROMPT` | メインセッション束縛の `todowrite` シム経由で `todos.db` |
| メインセッション削除時（purge 前） | session-end salvage（`salvage_session_memory`）：checkpoint transcript をスナップショットし、モデル/システムプロンプト/ツール面を捕捉し、通常の圧縮カットを読み取り専用で計算して、独立した 2 波の memory review をスケジュールする——第 1 波は破棄されるはずのプレフィックス、第 2 波は memory 再読込とプロンプト再構築後の保持尾部 | `memory` ツール経由で MEMORY.md / USER.md / FACTS.md |

## トリガ詳細

### 1. 圧縮時の memory review

`schedule_memory_review`（`agent/middlewares/summarization/background_reviews.py`、Summarization ミドルウェアがメッセージを実際に破棄する compact ごとに呼び出す）は、圧縮のたびに `_execute_memory_review(session_id, system_prompt, messages_to_summarize)` を fire-and-forget タスクとして派遣し、`background_review_memory_lock`（`state_register_mem`）の下で走らせる。渡されるのは**破棄される**スライス——失われようとしている事実はそこにあり、会話全体の一部にすぎない——で、既定の `cache_warm` モードでは**原文のまま**モデルへ送られる（`BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"` のときだけ `digest_history` で折り畳む）。いずれかのレビューロックが保持されている間、圧縮は派遣を完全にスキップする（キューイングなし）。

`_execute_memory_review`（`agent/middlewares/summarization/background_reviews.py`）は `_create_memory_review_agent` でレビュー agent を構築し、レンダリング済み `_MEMORY_REVIEW_PROMPT` を `HumanMessage` として破棄スライスに追加して呼び出す。**構造としてキャッシュ温存：** 圧縮の切れ目はプレフィックス切断であり、破棄スライスはシステムプロンプトの直後に位置する部分そのもの——同じモデル構成（`_build_session_model`：セッションの provider/model/thinking/temperature。グラフはターンごとに再構築されクライアントが閉じられるため、構成から**新規インスタンス**を構築）、同じシステムプロンプト、同じツール面で送り返せば、メインモデル自身のプレフィックスを再現し、プロバイダはプレフィックスキャッシュから応答します；新規入力はレビュー指示だけで、レビューが読むのは**実テキストのまま**です。プロンプトは、持続的なユーザー特性（ペルソナ、好み、個人的詳細）と振る舞いへの期待を `user` ターゲットへ、システムレベル / プロジェクト横断の落とし穴を `facts` ターゲットへ（プロジェクト固有の事実は MEMORY.md または AGENTS.md へ） `memory` ツールで保存するよう求める。レンダリング時には現在の FACTS.md 内容と文字上限（`_render_prompt_facts`）が渡され、エディタのように維持すること——同一の落とし穴を統合し、陳腐化した項目を置換し、ファイルを膨らませるより何も保存しないことを優先する——を指示する。保存対象がなければ "Nothing to save." と答えて停止させる。

- レビュー agent は**補助 LLM** 上の独立した `create_agent` で、ミドルウェアは `[_BackgroundReviewLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`、checkpointer なし。
- `_BackgroundReviewLimitTool`（`allowed_metadata_key` 未指定）は metadata に `background_review: True` を持つツールだけを通す。`memory` と `knowledge` がこのマークを持ち（ほかに対話用 `question` とプランモードの 2 つ）、memory review は memory を書けるが任意のメインツールは呼べない。スキル三点セット（`skill_list` / `skill_view` / `skill_manage`）は**意図的に未マーク**——スキル抽出はプラン完了パスの担当で、そちらはタグではなく明示的なツール面をバインドする。
- fork のメッセージはログのみ。`res["messages"]` の内容はメイングラフへ一切入らない。

### 2. スキルレビュー（todo 完了、ターン境界）

`_detect_todo_all_complete(session_id)` はプラン完了の可能性がある各ターンで評価され、完了サイクルごとに一度発火する：todos が存在し、すべて `completed` / `cancelled` で、`skill_review_fired` が未設定。fired フラグを立て、リストが不完全になるとリセットする。読み取りは fail-open。完了フックは毎ターン確認するため、短いセッションでも抽出できる。

フラグだけでは completed todo の in-place 編集を見逃すため、`_plan_snapshot_hash`（id + content + status + plan_ref）を前回**成功** pass の `skill_review_snapshot_hash` と比較する。内容が変われば再抽出し、変化がなければスキップ、失敗 pass は cooldown 後に再試行する。

`skill_review_enabled` と `skill_review_enabled` が有効なとき、hook は BACKGROUND_REVIEW レーンで `_run_skill_review` を fire-and-forget 起動する：

1. `_build_plan_context` は plan ファイル、todos、子 Agent runs（result_text 24 KB 上限）、および有界 `feedback`（最近の `skill_view` 呼び出し + `.usage.json` の最近項目 = `loaded_skills`、最近ターンのユーザー修正 = `user_corrections`）を集める。internal carrier、system reminder、workspace notice は除外。todo がなければ `{}` を返し pass をスキップ。
2. LLM の前に生の merged handoff tree を `handoff-tree.json` に原子的に書き、監査用に残す。モデル向け知識の読み取り口ではない。
3. `_SKILL_REVIEW_PROMPT` を圧縮コンテキストでレンダリングし、専用 agent（補助モデル、temperature=0、**会話なし**）に渡す。ツール面は `skill_list` / `skill_view` / `skill_manage` のみ。`knowledge` と `memory` はなく、知識や FACTS は書けない。
4. Prompt は証拠を明示的にマッピングする：`success_steps` → 手順/検証、`failed_branches` → pitfalls / `when_not_to_use`、`hard_constraints` → guardrails、`locked_decisions` → rationale、`user_corrections` → body / `when_to_use`。今回ロードした skill、既存 class-level umbrella、support file、最後に新規 class-level skill の順。具体的理由がある no-op だけ許可。

実装: `summarization/background_reviews.py` の `_run_skill_review` は `_create_skill_review_agent` を `skill_review_lock` 下で使います。`state_register_db` が指紋を保存し、`outcome` は subagent runs から来て、失敗フラグは `False` です。

スキルレビューは snapshot/judge トランザクションで走る：

- **snapshot**：`skill_review.snapshot_auto_skills()` が最初の試行前に `skills/auto`（skill、support file、usage sidecar）を捕捉。
- **write**：レビューが skill を作成/修補。`set_current_write_origin("background_review")` により新規 skill は agent-created として curator の管理対象になる。
- **judge**：`skill_review.judge_skill_update()` が補助モデルに `verdict`、`grounded_in_run`、`reusable`、`trigger_quality`、`quality`、`no_op_justified`、`feedback` を求める。`quality >= BACKGROUND_REVIEW_CONFIG["skill_review_quality_threshold"]`、`trigger_quality >= 6`、grounded、reusable が通過条件。no-op は `no_op_justified` が必要。
- **retry/rollback**：拒否された試行は pre-review snapshot に戻し、judge feedback 付きで再試行（既定最大 2 回）。使い切ると再度復元し、`skill_review_failed_hash` と時刻を記録して成功 fingerprint を進めない。後続ターンで `skill_review_failure_cooldown_s` 後に再試行でき、plan が変われば即時再試行。

この経路は knowledge（`task-*.json` / `wave-*.json` / `plan-summary.json`）も FACTS も書かない。既存 knowledge ファイルはディスクに残るが legacy 扱い。モデル向け prompt block と `knowledge` ツールは retired。

### 3. 圧縮後 todo fork

同じ圧縮点で、`_schedule_compression_todo_update`（`summarization/core.py` -> `background_reviews.schedule_compression_todo_update`）が `update_todos_from_compaction` を fire-and-forget の `asyncio.create_task` としてスケジュールする。スケジューラのゲートは**すべて**を満たす必要がある：

- `compression_todo_update_enabled`（`SUMMARIZATION`、既定 `True`）；
- cut が実際にメッセージを破棄した；
- 当該セッションで `compression_todo_update_lock` が保持されていない；
- セッションに空でない todo リストがある；
- 実行中のイベントループが存在する（同期圧縮経路はスキップし、debug ログを出す）。

`update_todos_from_compaction` は バックグラウンドレビュー agent を走らせ、システムプロンプトは `_COMPRESSION_TODO_PROMPT`、ツールはちょうど 1 つ：`_build_main_session_todowrite(session_id)`。fork は現在の todo リストを破棄スライスと突き合わせ、実際に完了した項目を `completed`、放棄または置換された項目を `cancelled`、証拠のある新規作業を `pending` として追加し、**完全な**リストを 1 回の `todowrite` 呼び出しで書き戻す（デルタではなく全置換）。変化がなければそのまま書き戻す。

fork の結果メッセージはログのみ。メイングラフやその checkpointer には何も届かず、派生セッションのミドルウェア状態は `finally` でクリアされる。

### 4. セッション削除（メインのみ）

`purge_session` がメインセッションを破棄する前に、`salvage_session_memory` が圧縮後の checkpoint transcript、モデル、システムプロンプト、ツール面をスナップショットし、通常の圧縮カットを**読み取り専用**で計算して、独立した 2 波のバックグラウンド review をスケジュールする。削除要求はモデルを待たない：同期処理はスナップショットだけで、予算は 5 秒。サイドチャットとサブエージェントの子はこの経路に入らない。

- **第 1 波**は `messages[:cutoff]`——圧縮が破棄したはずのプレフィックス——を、キャプチャ済みのモデル、システムプロンプト、ツール面で処理する。圧縮時 review と同じ cache-warm 形状で、要約は生成せず、圧縮状態にも触れない。
- **第 2 波**は `messages[cutoff:]`——保持される末尾——を処理する。第 1 波が memory を書いた後、タスクは `MEMORY.md`/`USER.md`/`FACTS.md` を再読み込みし、システムプロンプトを非永続的に再構築し（セッションは削除済み）、`memory` のみのツール面で末尾を review する。再構築に失敗した場合はキャプチャ済みプロンプトへフォールバックする。
- 両波は BACKGROUND_REVIEW レーンで、波ごとの派生セッションキー（`<session_id>::session-end-salvage:1|2`）と checkpointer なしで走る。キーは `finally` でクリアする。`memory` ツールの `background_review_memory` metadata が実行ゲートなので、第 1 波は cache-warm のためにセッションツール面を再現できても、削除後に `knowledge` や plan ツールを呼ぶことはできない。
- スナップショット、プロンプト再構築、タイムアウト、モデル例外などの失敗はすべて記録して握り潰す。プロセス終了は未完了のバックグラウンド review を放棄しうる——即時削除のための受け入れたトレードオフである。設定：`BACKGROUND_REVIEW_CONFIG["session_end_review_enabled"]` と `BACKGROUND_REVIEW_CONFIG["session_end_review_timeout_s"]`（波ごと、既定 120 秒）。

## Curator（スキルキュレーション）

**frontmatter 契約（インデックスそのものがトリガー）。** `<available_skills>` が描画するのはスキルの frontmatter だけ——本文はロード後に読まれます——なので、これらのフィールドがルーティング信号です: `description`（能力の要約）、`when_to_use`（**必須**：これらの経路が作成・修正するすべてのスキルに、それを読み込むべき具体的なシナリオ／ユーザー意図を記載）、`when_not_to_use`（任意：除外条件と代替手段）。ローダーはこの 2 つのタグを CJK 対応のトークン予算（60 / 50 / 50 トークン）で描画し XML エスケープします。クライアントのスキルツールチップも同じ 3 フィールドを表示します。キュレーターはさらに狭い第 2 層を加えます: アンブレラの frontmatter がスキル全体を覆い、各 `##` サブ能力セクションは自身の `**When to use:**` 行で始まります（実際の除外がある場合のみ `**When NOT to use:**` も）。これによりモデルは `skill_view` 後に正しいセクションを選べます。決定的フォールバックも同じ形を書き、条件はマージ理由から抽出します。

Curator（`context_engine/curator/`）は `skills/auto/` スキルライブラリのライフサイクルを担うバックグラウンドパスであり、まさに上記の抽出経路 2 の書込先である。読み書きするのはスキルだけ——MEMORY.md / USER.md / FACTS.md と `todos.db` は対象外である。

**何か。** 定時 cron ではなくアイドルトリガーのオーケストレーター。サービスエントリポイントが `context_engine.curator.init()` でデーモンスレッド（`curator-timer`）を起動する（`server/__main__.py:140`；HTTP-only モードではスキップ、`server/__main__.py:66-73`）。スレッドは 3600 秒ごとに起床し `maybe_run_curator(idle_for_seconds=...)` を呼ぶ（`context_engine/curator/__init__.py:122-140`）。パッケージのインポートは副作用なし——スレッドを起動するのは `init()` だけ（`context_engine/curator/__init__.py:151-165`）。

**いつ実行されるか。** 2 つのゲートを両方通過したときだけレビューが実行される：

- `should_run_now()`——有効、一時停止でない、`last_run_at` が有効間隔を超過（`context_engine/curator/transitions.py:21-39`）。有効間隔は `curator.interval_hours`（既定 168 時間 / 7 日、`config/sherry_settings.py:43`）で、クライアントから `.curator_state` の `auto_interval_days` により 1〜5 日に上書きできる（`context_engine/curator/config.py:97-107`）；
- Agent のアイドル時間が `curator.min_idle_hours`（既定 2）以上（`context_engine/curator/orchestrator.py:108-115`）。ユーザーターンごとにこのアイドルタイマーはリセットされる（`server/service/messages.py:190`）。

UI からは `POST /curator/run` で強制実行でき、ワーカースレッドで `run_curator_review()` を呼ぶ（`server/trigger/http/curator.py:129-160`）。アーカイブはその隣に公開されています：`POST /curator/restore`（body `{"name": "<skill>"}` → `{success, message}`；未知・bundled/hub 遮蔽・移動先既存は HTTP 400）と `GET /curator/archived`（`{success, archived, count}`）で、どちらも curator 層の `restore_skill()` / `list_archived()` に支えられています（`server/trigger/http/curator.py:163-204`）。

**ライフサイクル規則。** `apply_automatic_transitions()`（`context_engine/curator/transitions.py:41-91`）は `skills/auto/**/SKILL.md` を走査し（`context_engine/curator/usage.py:161-180`）、各スキルについて：pinned はスキップ；`stale_after_days`（既定 30 日）活動なしで `stale` に；`archive_after_days`（既定 90 日）超過で `skills/.archive/` に**アーカイブ**され `archived` に；再び活動したスキル、および stale 窓内で未使用のスキルは再活性化する。**3 つの除去はすべて復元可能**：90 日遷移、統合元スキル（アーカイブ時に umbrella 名を記した `ABSORBED_INTO` マーカーを書き込み）、剪定されたスキルはすべて `skills/.archive/` に入り、`curator restore <name>` で戻せる。復元は同じステップで curator 側レコードを `active` に戻し（agent 側フォワーダーが agent テレメトリ記録も同期）、復元されたスキルは直ちに `apply_automatic_transitions` へ再参加して 4 分岐すべてに外れることがなくなります；同一秒のアーカイブ衝突は入れ子にならない平級ディレクトリ（`<skill>-<timestamp>`）になります。不可逆に削除するのは明示的な削除——`skill_manage(action="delete")` またはクライアントの削除エンドポイント——のみです。アーカイブは自動では空にならない——`curator restore` で復元するか手動で整理する。既定値は `config/sherry_settings.py:42-48`。

**LLM 統合。** `curator.consolidate` が有効（既定で有効）なとき、`run_curator_review()` は非 pinned スキルの候補リストを描画し（`context_engine/curator/review.py:69`）、メイン LLM（temperature 0.3）に重複する狭いスキルをクラスレベルの umbrella スキルへ統合させる（`CURATOR_REVIEW_PROMPT`、`context_engine/curator/review.py:16`）。新しい umbrella とそのサポートファイルは `skills/auto/` 配下へ生成・永続化される（`_generate_umbrella_skill`、`context_engine/curator/umbrella.py:86`；`_apply_consolidation`、`context_engine/curator/orchestrator.py:163`）。統合はこのパス唯一の LLM ステップで、失敗は捕捉され実行は継続する。

**4 本の抽出経路との関係。** 経路 2（圧縮時 skill review）が生産者である：`skill_manage` を通じて `skills/auto/` を作成・修正する。Curator はその同じ産出の下流メンテナであり、skill review が作ったスキルを遷移・統合・剪定する。残り 3 経路は `skills/auto/` に一切触れない（それぞれ MEMORY.md / USER.md / FACTS.md、`todos.db`、削除時の memory サルベージに書く）ため、Curator と交差しない。

**状態と境界。** 実行状態は `skills/.curator_state`（`context_engine/curator/constants.py:3`、`context_engine/curator/state.py` が読み書き）；各実行は `logs/curator/{timestamp}/` 配下に `run.json` + `REPORT.md` を書く（`context_engine/curator/constants.py:4`；`context_engine/curator/report.py:196-205`）。範囲は `skills/auto/` のみ——内蔵スキルには決して触れず、pinned スキルはすべての破壊的遷移を迂回し、パス全体はバックグラウンドスレッドで走り会話ターン経路を占めない。詳細：[curator/README.md](../../context_engine/curator/README.ja.md)。

## 隔離と安全

| ガード | 保護対象 |
|---|---|
| skill-review ツール面 | `skill_list` / `skill_view` / `skill_manage` のみ。knowledge/memory はありません。 |
| バックグラウンドレビュー metadata 許可リスト（`metadata["background_review"]`、`_BackgroundReviewLimitTool` 既定規則） | memory review と skill review は background_review 用にタグ付けされたツールしか呼べない。それ以外のメインツールはエラー `ToolMessage` を返し、実行されない。 |
| 圧縮 metadata 許可リスト（`metadata["todo_update"] is True`、`_BackgroundReviewLimitTool(allowed_metadata_key="todo_update")`） | 圧縮 fork は該当 metadata マーク付き `todowrite` シムだけを許可する。 |
| 派生セッションキー `<id>::compression-todo` | 圧縮 fork の `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 状態キーがメインセッションと衝突しない。fork 実行中、メインセッションは `awrap_model_call` の最中だからである。`compression_todo_update_lock` だけが意図的にメインセッションへ書かれ、クロスパス再入コーディネータとして働く。 |
| 派生セッションキー `<id>::session-end-salvage:1|2` | 削除後の 2 波 review は波ごとの派生キーで走り、エンジン状態は `finally` でクリアされる。削除済みセッションの state を復活させない。タスク集合 `_SESSION_END_REVIEW_TASKS` が強参照を保持する。 |
| `background_review_memory` 実行ゲート（`metadata["background_review_memory"] is True`、`_BackgroundReviewLimitTool(allowed_metadata_key="background_review_memory")`） | 第 1 波は cache-warm のためにセッションツール面を再現できるが、実行できるのは `memory` だけ。削除後は `knowledge` と plan ツールが拒否される。 |
| メインセッション束縛の `todowrite` シム | fork グラフは派生キーで走るため、状態注入された実 `todowrite` は誤ったセッションを解決してしまう。シムは実ツールの `args_schema` と `description` を逐語的に再利用し（スキーマドリフトゼロ）、注入 `session_id` を捨て、構築時に捕捉したメインセッション id を束縛する。 |
| 読み取り専用 fork、checkpointer なし | 各バックグラウンドレビュー / スキルレビュー fork の結果メッセージはログのみで破棄される。fork に checkpointer はなく、メイングラフ状態を書けない。 |
| セッション単位の再入ロック | `background_review_memory_lock`、`skill_review_lock`、`compression_todo_update_lock` が同一抽出経路の重複実行を防ぐ。レビューロック保持中、圧縮スケジューラは派遣を完全にスキップする（キューイングなし）。 |
| fail-open 境界 | 各経路は `try/except` で作業を包み、ログして返す。抽出失敗がターン、圧縮、他の抽出へ伝播しない。 |
| バックグラウンドタスクの参照保持 | `_COMPRESSION_TODO_TASKS` と `_SESSION_END_REVIEW_TASKS` 集合が強参照を保持し、asyncio が実行中タスクを GC しないようにする。 |

## ストレージと上限

| ストア | パス（定数） | 上限 |
|---|---|---|
| `memory` target | MesMemory `memory_entries` 行（`target='memory'`） | 2200 文字（`MEMORY_TOOL["memory_char_limit"]`、`/memory/settings` で上書き可） |
| `user` target | MesMemory `memory_entries` 行（`target='user'`） | 1375 文字（`MEMORY_TOOL["user_char_limit"]`、上書き可） |
| `facts` target | MesMemory `memory_entries` 行（`target='facts'`、唯一のグローバルターゲット） | 1375 文字（`MEMORY_TOOL["facts_char_limit"]`、上書き可）；`add` が上限超過時は最も使われていない項目をアーカイブ |
| legacy plan knowledge | `workspace/knowledge/plans/<plan_key>/` | 廃止：旧 `task-<n>.json` / `wave-<n>.json` / `plan-summary.json` はディスクに残るが、モデル向け経路は読み書きしない |
| handoff audit | `workspace/knowledge/plans/<plan_key>/handoff-tree.json` | レビュー前に書く生 handoff tree。prompt block ではない |
| スキル | `config.path.AUTO_SKILLS_DIR`（`skills/auto/`） | `SKILL.md` と `references/` / `templates/` / `scripts/` のサポートファイル。PTC は `ptc__<plan_key>__<step_id>.py`、LDW は `ldw__<plan_key>.py` |
| スキル | `config.path.AUTO_SKILLS_DIR`（`skills/auto/`） | `SKILL.md` と `references/`、`templates/`、`scripts/` サポートファイル |
| todos | `agent/tools/todolist/data/todos.db`（`store_sqlite._DB_PATH`） | セッションスコープのリスト、全置換書込 |

## 設定スイッチ

| キー | 場所 | 既定値 | 効果 |
|---|---|---|---|
| `compression_todo_update_enabled` | `SUMMARIZATION`（`config/features/agent_side/summarization.py`） | `True` | 圧縮後 todo fork を有効化 |
| `skill_review_enabled` | `BACKGROUND_REVIEW_CONFIG`（`config/features/agent_side/background_review.py`） | `True` | スキルレビュー主スイッチ（skill-only、knowledge を書かない） |
| `skill_review_max_attempts` | `BACKGROUND_REVIEW_CONFIG` | `2` | 初回 + judge feedback 付き最大 1 回 |
| `skill_review_quality_threshold` | `BACKGROUND_REVIEW_CONFIG` | `6` | 通過に必要な最低 `quality` |
| `skill_review_feedback_turns` / `_max_items` / `_chars` | `BACKGROUND_REVIEW_CONFIG` | `12` / `8` / `2000` | 有界のユーザー修正コンテキスト |
| `skill_review_loaded_skills_max` | `BACKGROUND_REVIEW_CONFIG` | `20` | loaded-skill リスト上限 |
| `skill_review_failure_cooldown_s` | `BACKGROUND_REVIEW_CONFIG` | `3600` | 同一失敗 plan snapshot の再試行クールダウン |
| `session_end_review_enabled` | `BACKGROUND_REVIEW_CONFIG` | `True` | メインセッション削除時のサルベージを有効化 |
| `session_end_review_timeout_s` | `BACKGROUND_REVIEW_CONFIG` | `120` | 削除サルベージの波ごとのモデル呼び出し予算 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 実際の圧縮後の能動的圧縮クールダウン |
| `memory_char_limit` / `user_char_limit` / `facts_char_limit` | `MEMORY_TOOL`（`config/features/agent_side/memory_tool.py`） | `2200` / `1375` / `1375` | MEMORY.md / USER.md / FACTS.md の上限 |

## 検証

対象テスト：

検証メモ: Persistence assertions live in `tests/agent/middlewares/message_persistence/`。`skill_view` のロード記録、`test_skill_review.py`、`test_skill_review_feedback.py`、`test_skill_review_gate.py` を追加。



```bash
uv run pytest \
    tests/agent/middlewares/test_session_end_review.py \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_background_reviews.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/system_prompt/test_skill_review.py \
    tests/agent/middlewares/test_skill_review_module_notes.py \
    tests/agent/middlewares/test_skill_review_gate.py \
    tests/agent/middlewares/test_skill_review_feedback.py \
    tests/agent/middlewares/test_skill_review.py \
    tests/agent/tools/test_memory_store.py \
    tests/agent/tools/test_memory_store_facts.py -q
```

- `test_session_end_review.py`：削除スナップショット、読み取り専用の圧縮カット、第 1 波 → プロンプト再構築 → 第 2 波の順序、cutoff/空/スイッチ/重複/失敗の各分岐、派生状態のクリア、両波が MEMORY.md/USER.md に届くことを検証する。
- `test_compression_todo_update.py`：トリガゲート、fire-and-forget スケジュール、再入ロック、fail-open 解放、プロンプト内容、`todo_update` metadata ゲート、および完全 fork 隔離（派生キー、メインセッション `todowrite` シム、checkpointer / メッセージ漏洩なし）。
- `test_compression_background_reviews.py`：圧縮時ディスパッチ（memory review が compact 接縫から発火；カットなし圧縮は何もディスパッチしない）。`test_memory_review_digest.py` が破棄スライスの配線、cache_warm のモデル再構築（descriptor のフィールド、環境既定へのフォールバック、セッションのツール面）、`memory_review_mode="digest"` での折り畳みを固定します。永続化アサーションは `tests/agent/middlewares/message_persistence/` にあります。
- `test_compression_cooldown_persist.py`：クールダウンの再起動間生存。
- `test_skill_review_trigger.py`：`_detect_todo_all_complete` の 4 分岐、`schedule_memory_review` の圧縮ごとの memory review 派遣とロック意味論、`_build_plan_context`。
- `test_skill_review_module_notes.py`：`<module>-notes` carry-forward ガイダンス、スキルレビュープロンプトへのライブ FACTS.md レンダリング、およびスタブ LLM の挙動（create は `skills/auto/<module>-notes/` に着地、既存同名は patch され重複しない）。
- `test_skill_review_gate.py`：snapshot/restore ロールバック、変更パス検出、judge の pass/retry/no-op/unavailable 判定。
- `test_skill_review_feedback.py`：有界なユーザー修正、内部キャリアの除外、`skill_view` のロード済みスキル抽出と usage fallback。
- `test_skill_review.py`：judge 承認済み no-op、feedback 付き再試行、スナップショット復元、試行枯渇時の失敗マーカーと失敗クールダウン。
- Persistence assertions live in `tests/agent/middlewares/message_persistence/`.
- `test_memory_store.py`：MEMORY.md / USER.md ストアの意味論。
- `test_memory_store_facts.py`：`facts` ターゲット——add/replace/remove、上限超過時の最古項目ロールオフ、原子的書込、ロード時のファイル作成、ライブ内容レンダリング、プロンプトスナップショット注入（空ファイル → ブロックなし）。

AI 判定評価：`evals/skill_review/suite.py` が完了済み plan の実行（plan ファイル、完了 todos、合成子 Agent 実行記録）を用意し、実 `_run_skill_review` を呼び、続いて補助 LLM judge に、生成されたスキルが今回の実行に真に根ざし、再利用可能で、汎用的でないかを判定させる。すべての書込はサンドボックスへリダイレクトされ、実 `skills/auto/` と `workspace/` は一切触れられない。

```bash
uv run python evals/evals.py skill_review
```

スイートは `knowledge_retired`、`skills_created`、`ai_judge_skill_review`、`no_repo_pollution` を検査する。

## 既知の境界

- **圧縮 fork には `todoread` を与えない。** `todo_update` マーク付き `todowrite` シムだけを許可する。fork は現在のリストをプロンプトで受け取るため、`todoread` は意図的に未タグのままにする（`agent/tools/todolist/tools/__init__.py`）。
- **FACTS.md は単一ファイルのメモリである。** MEMORY.md / USER.md と同じ機構（単一ファイル、`§` 区切り項目、原子的リネーム書込、ファイルごとの文字上限）を共有し、毎回のシステムプロンプトへ注入される。階層型 / 分類型ストアではなく、上限超過時は最古の項目からロールオフする。
- **クールダウンは能動的圧縮だけを抑制する。** `compaction_cooldown_rounds`（3）は実際の圧縮後に T1 / T2 / T3 圧縮を止める。T4 / T5 のプロバイダエラー回復は、クールダウン、ターンごとの試行上限、その他の反スラッシュゲートを構造上バイパスする。

## 関連ドキュメント

- [セッションメモリ設計](../session_memory/README.md)：これらのストアを支えるセッションメモリ設計。
- [要約圧縮](../summarization/README.md)：圧縮トリガと、バックグラウンドレビューおよび todo fork をゲートするクールダウン。
- [長時間タスク](../long-running-tasks/README.md)：TaskFlow と、skill review が読み取る todo 計画層。
- [ミドルウェア README](../../agent/middlewares/README.md)：`@dynamic_prompt` システムプロンプト注入と Summarization のリファレンス。
- [Curator README](../../context_engine/curator/README.ja.md)：上記のバックグラウンドスキル保守オーケストレーター。
