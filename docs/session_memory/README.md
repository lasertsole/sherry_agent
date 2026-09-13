# Session Memory Architecture (SESSION Plan)

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

Implementation status of `TODO/SESSION_MEMORY_BORROWING_PLAN.md` — session-memory capabilities borrowed from opencode-dev, oh-my-openagent, hermes-agent and openclaw. Design rule: every new capability extends the existing long-running-task infrastructure (tiered facts, session continuity, state registers) — never a parallel store.

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

## Roadmap (pending)

P1-1 compression checkpoint rollback · P1-4 cross-session recall · P1-5 transcript tree & message branching · P2-1 event-sourcing migration · P2-2 context epoch snapshots · P2-3 dual-watermark facts extraction (must land on the existing tiered facts store) · P2-5 vector semantic search.

## Testing

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py tests/agent/middlewares/test_compression_cooldown_persist.py tests/context_engine/store/test_add_messages_idempotency.py -q
```
