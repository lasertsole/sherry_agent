# 🧪 プロダクト側の評価実践知見

> [English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · 日本語

> 6 つの仕組みは評価ハーネスの強化（DeepSWE / external-bench）から始まり、今はプロダクトの一等級の振る舞いです。どれも post-task または fail-open で、ターンを壊しません。すべて実測された評価失敗モードに由来します。信頼できる情報源：各節に記載したファイル。

## 1. リーフタイムアウト 1800 秒（`agent/tools/subagent/config.py`）

`run_timeout_seconds` のデフォルトを **1800 秒**（従来 600 秒）に変更。DeepSWE の実測では 600 秒で 20 問中 9 問、7200 秒で 20/20——ボトルネックはモデル能力ではなく、子プロセスが read-implement-test の途中で終了することでした。深度ラダーはリーフより 1 階層上ごとに 3 倍（depth-1 = 5400 秒）。`evals/deepswe/suite.py` は `_EVAL_CHILD_RUN_TIMEOUT_MULTIPLIER = 1.0`（プロダクト既定で評価予算をカバー、追加 ×3 不要）。明示的な上書き値は最小 600 / 最大 7200 でクランプされます。

## 2. Memory 文字上限の CJK 加重（`agent/tools/memory.py`）

Memory の文字上限（2200 / 1375 / 1375）は `_weighted_char_count` により、CJK 1 文字を `chars_per_token / chars_per_token_cjk`（4/2 = 2）個の加重文字として数えます。`pub/func/cjk.py::count_cjk` とトークン見積もり設定を再利用。ASCII 容量は不変、CJK は 2 倍のトークンを超えず同じ予算に収まります。すべての上限チェック（add / replace / evict / `_append_to_target`）とユーザー向け `[n% — X/Y chars]` 表示が加重カウントを使用。空・失敗時は fail-open のままです。

## 3. ターン間 diff-hash 無進捗検出（`agent/tools/todolist/stagnation_tracker.py`、`agent/middlewares/todo_continuation/core.py`）

計画テキストの停滞検出では、todo を編集し続けながらコードパッチが一切変わらない「closure-flow 空転」を検出できません。continuation ターン終了ごとに、enforcer はイベントループ外でステージ済み＋未ステージのバイナリ git diff をハッシュします（`_working_tree_diff_hash`）。連続 `TODOLIST_INFRA["diff_stagnation_threshold"]`（既定 2）回ハッシュが同じで、**step 子が在飛でない**場合、`[SYSTEM DIRECTIVE: NO PROGRESS DETECTED]` を注入して停止をラッチします。実ユーザーのターンや子の在飛はベースラインをリセット。非 git ディレクトリは空ハッシュ（fail-open、誤検出なし）。計画テキスト停滞検出と並行し、どちらかが発火するとゲートを停止します。

## 4. Verify 完了前の settle 待機（`config/features/agent_side/todolist_infra.py`、`agent/middlewares/todo_continuation/core.py`）

step 子は `done` を報告してもファイル編集がまだ書き込み中の場合があります。その瞬間に verify を完了すると偽の完了になります。reviewer が完了し step 子が在飛の場合、ゲートは `verify_settle_wait_s`（既定 30 秒、評価では 180 秒）まで settle を待ちます。タイムアウト時は verify 指令に `[WARNING: SUB-TASKS STILL RUNNING]` を追加——完了はブロックせず、パッチが不完全な可能性をモデルに伝えます。フロー読み取り失敗は「settle 済み」に降格（fail-open）。

## 5. Reviewer 受け入れチェックリスト（`agent/tools/taskflow/checklist_extractor.py`、`agent/tools/taskflow/tools/_dispatch.py`）

`functional_role="reviewer"` で spawn された reviewer は、task 末尾に番号付き PASS/FAIL の **ACCEPTANCE CHECKLIST** を受け取ります。規範的な文（must / should / shall / raise / reject / only / never / charset / return / error など）から抽出し、上限 20 件・重複排除・大小文字無視。`TASKFLOW_INFRA["reviewer_checklist_enabled"]`（既定オン）で制御。task テキストのみ変更——reviewer のモデル・ツール・隔離コンテキストは不変です。冪等：checklist ラベル済みの task は再追加しません（評価側 `set_reviewer_checklist` が優先）。抽出失敗は fail-open。

## 6. Session 実行レポート（`pub/func/session_report/**`、`server/service/session_report_service.py`、`server/trigger/http/session_report.py`）

3 つの post-task アナライザーを評価側からプロダクト共通層へ移動——熱経路にフックを挿しません。

- **token tracker**——`usage_metadata` をメイン＋役割別子エージェントに集約。USD コストは `SHERRY_SESSION_REPORT_TOKEN_PRICES`（または旧評価変数）が有効な価格ファイルを指す場合のみ出力。
- **context analyzer**——呼び出しごとにツール呼び出しを HIT/WASTED で採点。`hit_rate = effective / (effective + wasted)`。計画/オーケストレーション ツールは対象外、判定不能は HIT（保守的）。
- **code-intel tracker**——精密検索（LSP / explore / callers / semantic）と粗暴検索（search_files / read_file）の比率、fail-open の ToolMessage テキストから降格呼び出しを検出。

レポートは初回 API 読み取り時に生成されプロセス内にキャッシュされます（ターン終了時のバックグラウンドタスクや state register 書き込みなし）。キャッシュ未ヒット時は `GET /sessions/:session_id/report`（または `?session_id=`、`?refresh=1`）がその場で生成。クライアントはコンテキストビューアで折りたたみパネル表示（`client/app/components/chat/SessionReport.vue`）。プロセス再起動後は次回読み取りでキャッシュを再構築します。

## 安全不変条件

- 6 つすべて**追加型**：既存のプロンプト契約・ツールスキーマ・状態リテラル・ターンライフサイクルフックを非互換に変更していません（レポートキャッシュはプロセス内で、state register の key は追加していません）。
- すべての読み取り経路が **fail-open**：git 不在、store 故障、抽出失敗、transcript 不可はすべて「シグナルなし」に降格し、ハード失敗しません。
- しきい値は `TODOLIST_INFRA` / `TASKFLOW_INFRA` に集約。熱経路にマジックナンバーはありません。
