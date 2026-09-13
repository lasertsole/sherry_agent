# セッションメモリ設計（SESSION プラン）

[English](README.md) · [中文](README.zh.md) · 日本語 · [한국어](README.ko.md)

`TODO/SESSION_MEMORY_BORROWING_PLAN.md`（opencode-dev / oh-my-openagent / hermes-agent / openclaw から借用）の実装状況。設計ルール：新機能は既存 long-running-task 基盤（階層型 facts、セッション連続性、ステートレジスタ）の拡張として実装し、並列ストアは作らない。

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

## ロードマップ（未実装）

P1-1 圧縮チェックポイント復元 · P1-4 セッション横断リコール · P1-5 転録ツリーと分岐 · P2-1 イベントソーシング移行 · P2-2 Context Epoch スナップショット · P2-3 双ウォーターマーク facts 抽出（既存の階層型 facts ストア上に実装） · P2-5 ベクトル意味検索。

## テスト

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py tests/agent/middlewares/test_compression_cooldown_persist.py tests/context_engine/store/test_add_messages_idempotency.py -q
```
