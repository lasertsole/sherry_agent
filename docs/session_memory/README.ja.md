# セッションメモリ設計（SESSION プラン）

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

SESSION メモリプランの全 14 機能（opencode-dev / oh-my-openagent / hermes-agent / openclaw から借用）に加え、LT-1…LT-8 の長時間タスク編成も実装済み。設計ルール：すべての機能は既存インフラ（階層型 facts、セッション連続性、ステートレジスタ、MesMemory マイグレーション）の拡張であり、並列ストアは作らない。

> 状態（2026-09-13）：プラン退役。本 README が参照先。

## 実装済み機能

| 項目 | 機能 | 場所 |
|---|---|---|
| P0-1 | 圧縮前メモリフラッシュ | `agent/middlewares/summarization/memory_flush.py` |
| P0-2 | 圧縮クールダウンの再起動間永続化 | `agent/middlewares/summarization/core.py` の `_COOLDOWN_PERSIST_KEYS` |
| P0-3 | SQLite 圧縮ロック（TTL、fail-open） | `agent/middlewares/summarization/compaction_lock.py`、移行 v10 |
| P0-4 | ツール出力の一行要約 | `pub/func/message/tool_output_prune.py` |
| P1-1 | 圧縮チェックポイント + 復元 | 移行 v15、`restore_compaction_checkpoint` |
| P1-2 | メッセージ冪等永続化 | 移行 v11（`idempotency_key` + 部分一意インデックス） |
| P1-3 | `context_eligible` 履歴投影 | 移行 v12、取得時に非対象行を除外 |
| P1-5 | メッセージツリー + ゼロコピー fork | 移行 v14（`parent_message_id`、`session_leafs`） |
| P2-1 | 追記型イベントログ + プロジェクタ | 移行 v16、`context_engine/events/` |
| P2-2 | Context Epoch スナップショット | 移行 v17（`context_epoch` テーブル）、`ContextEpoch` |
| P2-3 | 双ウォーターマーク facts 抽出 | `context_engine/facts/`、`TieredMemoryStore.add_fact` 経由で書込 |
| P2-4（一部） | steer/queue デュアル配信 | `steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| P2-5 | ベクトル意味検索 | 移行 v13、`context_engine/embeddings/`、`message_search --semantic` |
| LT-1…8 | TaskFlow 編成 | `docs/long-running-tasks/` |

## MesMemory マイグレーション（v10–v17）

| バージョン | スキーマ |
|---|---|
| v10 | `compression_locks` —— セッション単位圧縮ロック（主キーで相互排他） |
| v11 | `messages.idempotency_key` + 部分一意インデックス（クラッシュリトライ重複排除） |
| v12 | `messages.context_eligible` —— コンテキスト投影フラグ（既定 1） |
| v13 | `message_embeddings` —— 意味検索ベクトルインデックス |
| v14 | `messages.parent_message_id` + `session_leafs` —— メッセージツリー |
| v15 | `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` |
| v16 | `events` —— 追記型ログ、セッション単位の無欠番 `seq` |
| v17 | `context_epoch` —— システムコンテキストのベースライン / スナップショット |

## 主要コンポーネント

- **`context_engine/facts/`** —— 双ウォーターマークカーソル（`cursor.py`、`state_register.db` に永続化）、補助 LLM 抽出器（`extractor.py`、json_repair 解析 + カテゴリフォールバック）、キュー編成（`queue.py`）。`ContextEngineHook.aafter_agent` に fire-and-forget で接続され、facts は既存の `TieredMemoryStore.add_fact`（facts/*.md）経由で書き込まれる。
- **`context_engine/events/`** —— 追記型イベントログ（セッション単位の無欠番シーケンス：`types.py`、`store.py`）、チェックポイントイベントを P1-1 読みモデルへ写像する `EventProjector`。
- **`context_engine/embeddings/`** —— ベクトル意味検索：遅延 embed バックエンド（上書き可能）、冪等 LEFT-JOIN インデクサ、コサイン順位付け。`message_search` ツール（`semantic: true`）で公開。
- **`agent/tools/message_search.py`** —— 二段階検索：永続化済み `messages` テーブルの FTS5 を先に検索し、ヒットがない場合はセッションの最新チェックポイント（`SRC_DIR/checkpoints/sqlite.db` の `state["messages"]`）へ降格して、未永続化ターンを新しい順にキーワード一致（`_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars` で上限）。フォールバックのヒットには `source="checkpoint"` を付与。
- **`agent/middlewares/summarization/compaction_lock.py`** —— SQLite 圧縮ロック（TTL 自己修復、同期 + 非同期取得、タイムアウト時 fail-open）。
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
    tests/context_engine/facts/test_facts_extraction.py \
    tests/context_engine/embeddings/test_semantic_search.py \
    tests/agent/tools/test_message_search_checkpoint_fallback.py -q
```

## 評価

```bash
uv run python evals/evals.py session_memory
```

サンドボックス下でライブ子系统を採点——7 チェック：クールダウン再起動存活、ロック相互排除、チェックポイント復元、冪等リプレイ、コンテキスト投影、実 LLM facts 抽出、実 embed 意味順位付け。詳細は `evals/session_memory/suite.py`。
