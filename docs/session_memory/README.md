# Session Memory Architecture (SESSION Plan)

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

All 14 capabilities of the SESSION memory plan (borrowed from opencode-dev, oh-my-openagent, hermes-agent and openclaw) are implemented, plus the LT-1…LT-8 long-running-task orchestrations. Design rule: every capability extends the existing infrastructure — tiered facts, session continuity, state registers, MesMemory migrations — never a parallel store.

> Status (2026-09-13): plan retired. This README is the reference.

## Implemented Capabilities

| Item | Capability | Where |
|---|---|---|
| P0-1 | Pre-compression memory flush | `agent/middlewares/memory_flush.py` |
| P0-2 | Compression cooldown persisted across restarts | `_COOLDOWN_PERSIST_KEYS` in `agent/middlewares/summarization.py` |
| P0-3 | SQLite compaction lock (TTL, fail-open) | `agent/middlewares/compaction_lock.py`, migration v10 |
| P0-4 | One-line tool-output summaries | `pub/func/message/tool_output_prune.py` |
| P1-1 | Compaction checkpoints + restore | migration v15, `restore_compaction_checkpoint` |
| P1-2 | Idempotent message persistence | migration v11 (`idempotency_key` + partial unique index) |
| P1-3 | `context_eligible` history projection | migration v12; retrieval filters ineligible rows by default |
| P1-5 | Message tree + zero-copy forking | migration v14 (`parent_message_id`, `session_leafs`) |
| P2-1 | Append-only event log + projector | migration v16, `context_engine/events/` |
| P2-2 | Context epoch snapshots | migration v17 (`context_epoch` table), `ContextEpoch` |
| P2-3 | Dual-watermark facts extraction | `context_engine/facts/`, writes via `TieredMemoryStore.add_fact` |
| P2-4 (partial) | steer/queue dual delivery | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| P2-5 | Vector semantic search | migration v13, `context_engine/embeddings/`, `message_search --semantic` |
| LT-1…8 | TaskFlow orchestration | `docs/long-running-tasks/` |

## MesMemory Migrations (v10–v17)

| Version | Schema |
|---|---|
| v10 | `compression_locks` — per-session compaction lock (PRIMARY KEY = mutex) |
| v11 | `messages.idempotency_key` + partial unique index (crash-retry dedup) |
| v12 | `messages.context_eligible` — context projection flag (default 1) |
| v13 | `message_embeddings` — vector index for semantic search |
| v14 | `messages.parent_message_id` + `session_leafs` — message tree |
| v15 | `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` |
| v16 | `events` — append-only log, gapless per-session `seq` |
| v17 | `context_epoch` — system-context baseline / snapshot |

## Key Components

- **`context_engine/facts/`** — dual-watermark cursor (`cursor.py`, durable on `state_register.db`), auxiliary-LLM extractor (`extractor.py`, json_repair parsing + category fallback), queue orchestration (`queue.py`). Wired into `ContextEngineHook.aafter_agent` as a fire-and-forget background task; facts are written through the existing `TieredMemoryStore.add_fact` (facts/*.md).
- **`context_engine/events/`** — append-only event log with gapless per-session sequences (`types.py`, `store.py`), and an `EventProjector` mapping checkpoint events onto the P1-1 read model.
- **`context_engine/embeddings/`** — vector semantic search: lazy embed backend (project embed model, overridable), idempotent LEFT-JOIN-driven indexer, cosine ranking; exposed by the `message_search` tool (`semantic: true`).
- **`agent/middlewares/compaction_lock.py`** — SQLite compaction lock (TTL self-healing, sync + async acquire, fail-open on timeout), wrapping both `_apply_compression` and `_aapply_compression`.
- **`runtime/session/state_register.py`** — `ContextEpoch` lifecycle (initialize / prepare / replace / advance) over the `context_epoch` table.

## Testing

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/context_engine/store/test_add_messages_idempotency.py \
    tests/context_engine/store/test_compaction_checkpoints.py \
    tests/context_engine/store/test_message_tree.py \
    tests/context_engine/events/test_events.py \
    tests/runtime/test_context_epoch.py \
    tests/context_engine/facts/test_facts_extraction.py \
    tests/context_engine/embeddings/test_semantic_search.py -q
```

## Eval

```bash
uv run python evals/evals.py session_memory
```

Scores the live subsystem under the eval sandbox — 7 checks covering cooldown restart survival, lock mutual exclusion, checkpoint restore, idempotent replay, context projection, real-LLM facts extraction and real-embed semantic ranking. See `evals/session_memory/suite.py`.
