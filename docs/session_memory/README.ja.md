# セッションメモリ設計（SESSION プラン）

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

`the SESSION plan (retired)`（opencode-dev / oh-my-openagent / hermes-agent / openclaw から借用）の実装状況。設計ルール：新機能は既存 long-running-task 基盤（階層型 facts、セッション連続性、ステートレジスタ）の拡張として実装し、並列ストアは作らない。

## 実装済み

| 項目 | 機能 | 場所 |
|---|---|---|
| P0-1 | 圧縮前メモリフラッシュ | `agent/middlewares/memory_flush.py` |
| P0-2 | 圧縮失敗クールダウンの永続化 | `agent/middlewares/summarization.py` の `_COOLDOWN_PERSIST_KEYS` |
| P0-3 | SQLite 圧縮ロック（TTL、fail-open） | `agent/middlewares/compaction_lock.py`、MesMemory マイグレーション v10 |
| P0-4 | ツール出力の一行要約 | `pub/func/message/tool_output_prune.py` |
| P1-2 | メッセージ冪等永続化 | MesMemory マイグレーション v11（`idempotency_key` + 部分一意インデックス） |
| P1-3 | `context_eligible` 履歴投影 | MesMemory マイグレーション v12、取得時にデフォルト除外 |
| P2-4（一部） | steer/queue デュアル配信 | `steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| LT-* | TaskFlow DAG / 予算 / 締切 / リトライ / 連続性 | `docs/long-running-tasks/` |
| P1-1 | 圧縮チェックポイント + 復元 | MesMemory マイグレーション v15、`restore_compaction_checkpoint` |
| P1-5 | メッセージツリー + ゼロコピー fork | MesMemory マイグレーション v14（`parent_message_id`、`session_leafs`） |
| P2-1 | 追記型イベントログ + プロジェクタ | MesMemory マイグレーション v16、`context_engine/events/` |
| P2-2 | Context Epoch スナップショット | ランタイム移行（`context_epoch` テーブル）、`ContextEpoch` |
| P2-3 | 双ウォーターマーク facts 抽出 | `context_engine/facts/`（カーソル + 抽出器）、TieredMemoryStore |
| P2-5 | ベクトル意味検索 | MesMemory マイグレーション v13、`context_engine/embeddings/`、`message_search --semantic` |

## ロードマップ（未実装）

計画の 14 項目（LT-1…LT-8 含む）はすべて実装済み — 計画ドキュメントは役目を終え、本 README が参照先です。

## テスト

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py tests/agent/middlewares/test_compression_cooldown_persist.py tests/context_engine/store/test_add_messages_idempotency.py -q
```
