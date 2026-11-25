# 🛡️ プロダクト側の B クラス評価実践知見

> [English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · 日本語

> 評価ハーネスの観測（external-bench / DeepSWE）とプロダクトで実測された失敗モードから昇格した、4 つの fail-open 強化策です。A クラスと異なり、これらは可観測性／ガードレールです。いずれも既存ゲートの決定パスを変更せず、失敗時は no-op に低下します。信頼できる情報源：各節に記載したファイル。

## 1. PathGuard 拒否カウント（`agent/middlewares/path_guard/core.py`、`server/service/turn_runner.py`）

ブロックされたツール呼び出しは通常のエラー `ToolMessage` を返すため、エージェントが PathGuard の拒否でループすると、理由が見えないまま何百回もの呼び出しを生んでいました。PathGuard は同期・非同期の拒否分岐の両方で**セッションごとの拒否カウント**（`record_denial` → `_PATH_DENIALS`）を記録するようになりました。回数と重複排除した拒否パスです。ターン終了時に `on_turn_finished` がカウントを（一度だけ）ドレインし、タイトル `PathGuard 拒绝了写/读操作` の一時的な `system.notice` カードを、回数と最大 5 件のパスとともにプッシュします。この通知は既存のインライン通知ストリップを再利用するため、新しいソケットフレームやトーストハンドラは追加していません。セッション破棄時は `clear_denials` を呼び、残骸を残しません。決定パスは不変です。カウントは可観測性のためだけに使われ、空の session id は数えず、報告するものがなければドレインは `None` を返します。

## 2. ステップが進行中の待機のみ指令（`agent/middlewares/todo_continuation/core.py`）

プラン継続 enforcing は、TaskFlow のステップ子エージェントがまだ実行中でも「次の未完了タスクに取り組め」と言っていました。そのため、4 つの子が実行中のまま完了を宣言できました。`_has_in_flight_steps(flows)` が真（いずれかの step が `dispatched` または `ready`）の場合、ゲートは代わりに `[SYSTEM DIRECTIVE: STEPS IN FLIGHT]` を選択します。`taskflow_step(action='wait_all')` を呼び、settle した step を `resume` し、`taskflow_flow(action='finish')` でフローを閉じます。すべての子が settle するまで**完了を宣言したり新しい作業を始めたりしてはいけません**。進行中のステップがなければ通常の継続指令を使います。全ブロック状態には blocked-only が引き続き適用されます（進行中とブロックは排他的です）。ステータスブロックと board footer は保持されます。指令は決定を狭めるだけで、プランの他の部分を隠しません。

## 3. 圧縮時のリマインダスナップショット保持（`agent/middlewares/reminder_catalog/dynamic.py`、`agent/middlewares/summarization/compression.py`）

ターン中途の圧縮は cutoff ウィンドウ全体（内部のすべての `<system-reminder>` を含む）をサマリーペアに置き換えていたため、モデルは残りのターンを todo／TaskFlow のアンカーなしで実行していました。圧縮パイプラインは切り詰めの後（recovery context の書き換えの前）に `capture_reminder_snapshot` を呼び、結果をサマリーペアと保持テールの間に挿入します。`[summary, snapshot, preserved…]` です。スナップショットは source ごとの非 transient な最後のリマインダーの**圧縮時点**のバージョンであり、live state ではありません。そのためモデルはタイムラインを見ます。サマリー → 圧縮時の状態 → より新しい preserved リマインダー。transient source（current_time、lsp_diagnostics、project_instructions）は決してキャプチャしません。source ごとに、スナップショットが preserved の**最も古い**リマインダーとバイト単位で同一なら破棄します（純粋な冗長）。最新ではなく最古と比較することで、状態が変化して元に戻った場合にスナップショットを誤って削除するのを防ぎます。スナップショットは遅延インポートで、サマリーペアが実際に構築された場合のみ実行され、エラー時は「スナップショットなし」に fail-open します。動的リマインダーミドルウェア、そのガード、次のターンの注入は変更されません。

## 4. 並列ツールバッチの集計サイズガード（`agent/safe_tool_node.py`、`pub/func/message/eviction.py`、`config/features/agent_side/tool_parallel.py`）

`ContextEviction` はツールノード内で**呼び出しごと**にエビクトするため、20K 文字の閾値ぎりぎり下回る並列結果が N 個あると、すべて残って合計がコンテキストウィンドウを破裂させます。per-call フックは合計を見られません。`SafeToolNode` はバッチ全体を保持できる唯一の地点（`gather` の後、`_combine_tool_outputs` の前、enforce／non-enforce × sync／async の 4 経路すべて）で `_enforce_batch_size_limit` を適用します。合計が `TOOL_PARALLEL["batch_result_threshold_chars"]`（デフォルト 100K、0 で無効）を超えると、最大の結果から既存の `evict_tool_result`（ディスク offload ＋ head/tail プレビュー）でエビクトし、失敗すると `hard_clip_tool_result`（メモリ切り詰め）にフォールバックして、バッチが収まるかこれ以上縮められなくなるまで続けます。2 つの補助関数はこの経路で `force=True` キーワードを受け取り、19K の単一結果が 20K の per-call 閾値未満でもエビクトできるようにします。デフォルトの呼び出し側はバイト単位で不変です。エビクト済みの結果とサイズ 0 の項目はスキップされ、順序と個数は保持され、失敗時は常に元の結果を保持します。ディスクプレビューには約 4K 文字の下限があるため、超大バッチはすべてのシグナルを犠牲にして閾値以下にするのではなく、数倍に縮小されます。非同期経路は `asyncio.to_thread` でガードを実行し、イベントループを阻塞させません。既存の `read_file` 復元ヒントを `offset=1` に修正しました（このツールは 1-indexed です）。

## 安全性の不変条件

- 4 つすべてが**追加型**です。既存のプロンプト契約、ミドルウェア順序、ツールスキーマ、状態キー、ターンライフサイクルフックを非互換に変更していません。
- すべての読み取り／ガード経路は **fail-open** です。空の session id、空のカウント、store 故障、キャプチャエラー、offload 失敗は「シグナルなし／元の結果を保持」に低下し、ハード失敗にはなりません。
- 閾値は `TOOL_PARALLEL` に集約されています。wait-only ルールは既存の `_has_in_flight_steps` 判定を再利用し、スナップショットはリマインダーカタログの transient-source レジストリを再利用します。ホットパスに魔法の数値はありません。
