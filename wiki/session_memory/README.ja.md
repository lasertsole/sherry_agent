# セッションメモリ設計（SESSION プラン）

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

SESSION メモリプランの 13 機能のうち 12 機能（opencode-dev / oh-my-openagent / hermes-agent / openclaw から借用）が実装済みです——圧縮前メモリフラッシュは、圧縮時 memory review がその役割を包含するため退役しました——これに長時間タスク編成が加わります。設計ルール：すべての機能は既存インフラ（セッション連続性、ステートレジスタ、MesMemory スキーマ）の拡張であり、並列ストアは作らない。

> 状態（2026-09-13）：プラン退役。本 README が参照先。

## 実装済み機能

| 機能 | 場所 |
|---|---|
| 圧縮クールダウンの再起動間永続化 | `agent/middlewares/summarization/core.py` の `_COOLDOWN_PERSIST_KEYS` |
| SQLite 圧縮ロック（TTL、fail-open） | `agent/middlewares/summarization/compaction_lock.py`、現在の schema |
| ツール出力の一行要約 | `pub/func/message/tool_output_prune.py` |
| 圧縮チェックポイント + 復元 | 現在の schema、`restore_compaction_checkpoint` |
| メッセージ冪等永続化 | 現在の schema（`idempotency_key` + 部分一意インデックス） |
| `context_eligible` 履歴投影 | 現在の schema、取得時に非対象行を除外 |
| メッセージツリー + ゼロコピー fork | 現在の schema（`parent_message_id`、`session_leafs`） |
| 追記型イベントログ + プロジェクタ | 現在の schema、`context_engine/events/` |
| Context Epoch スナップショット | 現在の schema（`context_epoch` テーブル）、`ContextEpoch` |
| steer/queue デュアル配信 | `steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| ベクトル意味検索 | 現在の schema、`context_engine/embeddings/`、`message_search --semantic` |
| TaskFlow 編成 | `wiki/long-running-tasks/` |

## メモリエントリ

3 つの長期メモリターゲット（`memory`、`user`、`facts`）は MesMemory の `memory_entries` テーブルに 1 行 1 `§` エントリとして保存され、`MemoryEntryRepository`（`context_engine/store/memory_entry_repository.py`）経由で読み書きされます。旧 `workspace/memory/{MEMORY,USER,FACTS}.md` はレガシーなシードに過ぎません: `memory_migration.migrate_legacy_memory_files()` が `agent_core.init()` で 1 トランザクションとして 1 回だけインポートし、コミット後にのみ各ファイルを `*.md.bak` に改名します。既存行が 1 つでもあればインポートはスキップされるため、改名失敗でも重複しません。トランザクション失敗時はファイルを残し、次回起動で再試行します。

`MemoryStore.load_from_store()` は `created_at ASC, id ASC`（旧ファイルの上から下の順）でアクティブ行を読み、レンダリング結果をシステムプロンプトスナップショットとして凍結します。セッション中の書き込みは永続化されますがスナップショットは再描画せず、provider の prefix キャッシュを安定させます。スナップショットは次回セッション開始時、圧縮後の再構築時、モデル/思考切替時、サブエージェント完了の取り込み時に再読み込みされます。per-project FACTS は存在せず、旧 `<project>/.sherry/memory/FACTS.md` は無視されます。FACTS は引き続きシステムレベル / プロジェクト横断の落とし穴のみを保持し、memory review / plan extraction nudge がそのルーティングを徹底します。

各行は `origin_session`、`usage_count`、`last_used` を記録します。レンダリングブロックは由来が分かるエントリの後ろに `[origin: <sid>, created: <date>, used: <n>x]` を付けます。`memory(action="read")` は一致するエントリを返し使用回数を増やします。`facts` が溢れると**最も使われていない**エントリをアーカイブし（`COALESCE(last_used, created_at)`、次に `usage_count`）、削除はすべて復元可能なソフト削除です。記憶パネルは `/memory/entries*`、`/memory/settings`、`/memory/consolidate` を使い、旧 `GET/PUT /memory` は `§` 区切り形式を維持します。

| ターゲット | 用途 | 上限 | 保守 |
|---|---|---|---|
| `memory` | Agent 側ノート：環境事実、プロジェクト規約、ツールの癖 | 2200 文字（`MEMORY_TOOL["memory_char_limit"]`、パネルで上書き可） | `memory` ツール / memory review nudge |
| `user` | ユーザープロファイル：好み、コミュニケーション様式、期待 | 1375 文字（`MEMORY_TOOL["user_char_limit"]`、パネルで上書き可） | `memory` ツール / memory review nudge |
| `facts` | システムレベル / プロジェクト横断の落とし穴。毎回のシステムプロンプトに注入 | 1375 文字（`MEMORY_TOOL["facts_char_limit"]`、パネルで上書き可）、最も使われていないものからロールオフ | `memory` ツール / memory review・plan extraction nudge |

## MesMemory スキーマ

| スキーマオブジェクト | 用途 |
|---|---|
| `compression_locks` | セッション単位圧縮ロック（主キーで相互排他） |
| `messages.idempotency_key` + 部分一意インデックス | クラッシュリトライ重複排除 |
| `messages.context_eligible` | コンテキスト投影フラグ（既定 1） |
| `message_embeddings` | 意味検索ベクトルインデックス |
| `memory_entries` / `memory_settings` | 長期メモリ行（ターゲット、由来、使用量、アーカイブ）と UI 編集可能な上限 |
| `messages.parent_message_id` + `session_leafs` | メッセージツリー |
| `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` | 圧縮復元ポイント |
| `events` | 追記型ログ、セッション単位の無欠番 `seq` |
| `context_epoch` | システムコンテキストのベースライン / スナップショット |

`build_schema_v1` がこの完全なスキーマを一度に作成します。バージョン付きマイグレーション連鎖はありません。

## 主要コンポーネント

- **`context_engine/events/`** —— 追記型イベントログ（セッション単位の無欠番シーケンス：`types.py`、`store.py`）、チェックポイントイベントをチェックポイント読みモデルへ写像する `EventProjector`。
- **`context_engine/embeddings/`** —— ベクトル意味検索：遅延 embed バックエンド（上書き可能）、冪等 LEFT-JOIN インデクサ、コサイン順位付け。`message_search` ツール（`semantic: true`）で公開。
- **`agent/tools/message_search.py`** —— 二段階検索：永続化済み `messages` テーブルの FTS5 を先に検索し、ヒットがない場合はセッションの最新チェックポイント（`SRC_DIR/checkpoints/sqlite.db` の `state["messages"]`）へ降格して、未永続化ターンを新しい順にキーワード一致（`_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars` で上限）。フォールバックのヒットには `source="checkpoint"` を付与。永続化が各モデル境界と各ツール返却時に走るため、このフォールバックが効くのは「チェックポイントがストアより先行している」狭い窓のみです —— 次のモデル境界で永続化されるのを待つ HITL 拒否（ツール結果は返却時にすでに書き込み済み）。
- **`agent/middlewares/message_persistence/`** —— write-once のセッション永続化、2 つのタイミング: human/AI メッセージは各モデル呼び出し境界で、ツール結果は返却の瞬間に。`persisted_message_ids` ウォーターマークが各メッセージをちょうど 1 回だけ着地させ、永続化は圧縮の発火に依存しません。
- **コンテキスト量の統治** —— `ContextEvictionMiddleware` は過大なツール結果（> 20 000 文字）を state に入る前に `SESSIONS_DIR/<session_id>/evicted/` へ退避します（state に残るのは head+tail プレビューのみ）。巨大な人間メッセージ（> 200 000 文字）も同じディレクトリへ書き出し `lc_evicted_to` でタグ付けしますが、切り詰めるのはモデルビューだけです——state と MesMemory は全文を保持します。`clear_session()` はセッションディレクトリごと削除するため、退避ファイルも一緒に消えます。P1-2 のオーバーフロー・テールクリップは末尾 `ToolMessage` の内容を `model_copy` でスタブ化するだけ（同一性とペアリングは不変）—— データは失われません。すべての結果はすでに MesMemory へ永続化済みで、退避された本文もディスク上に残るためです: `message_search` がテキストを呼び戻し、`read_file` が退避ファイルを読み直せます。
- **ターミナル stdout はパイプを通らない** —— `terminal` ツールは子プロセスの stdout を直接ファイルに向け（stderr は従来どおりそこへ統合）、終了後に一度だけ判定します。`TOOLS_TIMEOUTS["fd_write_threshold_bytes"]`（100 000 バイト）未満なら一時ファイルを丸ごと読んで削除し、それ以上なら `evicted/terminal-<sha256(先頭 64 KiB)[:16]>.log` へリネームして、バイト総量（`stat().st_size` ——行数を数えるためにファイルを走査することはありません）を記した head/tail プレビューを返します。プレビュー自体もバイト上限付きなので、改行のない 500 MB の出力でも数 KB で済みます。タイムアウトやキャンセルされたコマンドは自分の一時ファイルを削除し、強制終了の残骸は `atomic_write.py` の共通の `.sherry-tmp-` / `.log` 規約で掃除されます。
- **`agent/middlewares/summarization/compaction_lock.py`** —— SQLite 圧縮ロック（TTL 自己修復、同期 + 非同期取得、タイムアウト時 fail-open）、`_apply_compression` と `_aapply_compression` の両方をラップします。
- **`runtime/session/state_register.py`** —— `context_epoch` テーブル上の `ContextEpoch` ライフサイクル（initialize / prepare / replace / advance）。

## テスト

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/context_engine/store/test_add_messages_idempotency.py \
    tests/context_engine/store/test_compaction_checkpoints.py \
    tests/context_engine/store/test_message_tree.py \
    tests/context_engine/events/test_events.py \
    tests/runtime/test_context_epoch.py \
    tests/context_engine/embeddings/test_semantic_search.py \
    tests/agent/tools/test_message_search_checkpoint_fallback.py -q
```

## 評価

```bash
uv run python evals/evals.py session_memory
```

サンドボックス下でライブサブシステムを採点——6 チェック：クールダウン再起動後の生存、ロック相互排除、チェックポイント復元、冪等リプレイ、コンテキスト投影、実 embed 意味順位付け。詳細は `evals/session_memory/suite.py`。
