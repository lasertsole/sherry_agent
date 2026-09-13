# Session Memory Architecture (SESSION Plan)

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

Implementation status of `the SESSION plan (retired)` — session-memory capabilities borrowed from opencode-dev, oh-my-openagent, hermes-agent and openclaw. Design rule: every new capability extends the existing long-running-task infrastructure (tiered facts, session continuity, state registers) — never a parallel store.

## Implemented

| Item | Capability | Where |
|---|---|---|
| P0-1 | Pre-compression memory flush | `agent/middlewares/memory_flush.py` |
| P0-2 | Compression-failure cooldown persisted across restarts | `_COOLDOWN_PERSIST_KEYS` in `agent/middlewares/summarization.py` |
| P0-3 | SQLite compaction lock (TTL, fail-open) | `agent/middlewares/compaction_lock.py`, MesMemory migration v10 |
| P0-4 | One-line tool-output summaries | `pub/func/message/tool_output_prune.py` |
| P1-2 | Idempotent message persistence | MesMemory migration v11 (`idempotency_key` + partial unique index), `add_messages` marker |
| P1-3 | `context_eligible` history projection | MesMemory migration v12; retrieval filters ineligible messages by default |
| P2-4 (partial) | steer/queue dual delivery | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn`; child steer via `sessions_steer` |
| LT-* | TaskFlow DAG / budget / deadline / retry / continuity | `docs/long-running-tasks/` |
| P1-1 | Compaction checkpoints + restore | MesMemory migration v15, `restore_compaction_checkpoint` |
| P1-5 | Message tree + forking (zero-copy) | MesMemory migration v14 (`parent_message_id`, `session_leafs`) |
| P2-1 | Append-only event log + projector | MesMemory migration v16, `context_engine/events/` |
| P2-2 | Context epoch snapshots | runtime migration (`context_epoch` table), `ContextEpoch` |
| P2-3 | Dual-watermark facts extraction | `context_engine/facts/` (cursors + extractor), TieredMemoryStore |
| P2-5 | Vector semantic search | MesMemory migration v13, `context_engine/embeddings/`, `message_search --semantic` |

## Roadmap

All 14 plan items (plus LT-1…LT-8) are implemented — the plan document is retired; this README is the reference.

## Testing

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py tests/agent/middlewares/test_compression_cooldown_persist.py tests/context_engine/store/test_add_messages_idempotency.py -q
```
