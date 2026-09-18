# Session Memory Architecture (SESSION Plan)

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

All 13 capabilities of the SESSION memory plan (borrowed from opencode-dev, oh-my-openagent, hermes-agent and openclaw) are implemented, plus the long-running-task orchestrations. Design rule: every capability extends the existing infrastructure — session continuity, state registers, MesMemory migrations — never a parallel store.

> Status (2026-09-13): plan retired. This README is the reference.

## Implemented Capabilities

| Capability | Where |
|---|---|
| Pre-compression memory flush | `agent/middlewares/summarization/memory_flush.py` |
| Compression cooldown persisted across restarts | `_COOLDOWN_PERSIST_KEYS` in `agent/middlewares/summarization/core.py` |
| SQLite compaction lock (TTL, fail-open) | `agent/middlewares/summarization/compaction_lock.py`, migration v10 |
| One-line tool-output summaries | `pub/func/message/tool_output_prune.py` |
| Compaction checkpoints + restore | migration v15, `restore_compaction_checkpoint` |
| Idempotent message persistence | migration v11 (`idempotency_key` + partial unique index) |
| `context_eligible` history projection | migration v12; retrieval filters ineligible rows by default |
| Message tree + zero-copy forking | migration v14 (`parent_message_id`, `session_leafs`) |
| Append-only event log + projector | migration v16, `context_engine/events/` |
| Context epoch snapshots | migration v17 (`context_epoch` table), `ContextEpoch` |
| steer/queue dual delivery | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| Vector semantic search | migration v13, `context_engine/embeddings/`, `message_search --semantic` |
| TaskFlow orchestration | `docs/long-running-tasks/` |

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

- **`context_engine/events/`** — append-only event log with gapless per-session sequences (`types.py`, `store.py`), and an `EventProjector` mapping checkpoint events onto the checkpoint read model.
- **`context_engine/embeddings/`** — vector semantic search: lazy embed backend (project embed model, overridable), idempotent LEFT-JOIN-driven indexer, cosine ranking; exposed by the `message_search` tool (`semantic: true`).
- **`agent/tools/message_search.py`** — two-stage recall: FTS5 over the persisted `messages` table first; when there is no hit it falls back to the session's newest checkpoint (`SRC_DIR/checkpoints/sqlite.db`, `state["messages"]`), keyword-matching not-yet-persisted turns newest-first (bounded by `_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars`); fallback hits are tagged `source="checkpoint"`. Since persistence now runs at every model boundary and on every tool return, this fallback only matters in the narrow window where the checkpoint runs ahead of the store — HITL denial messages waiting for the next model boundary to persist them (tool results are already written the moment they return).
- **`agent/middlewares/message_persistence/`** — write-once session persistence at two timings: new human/ai messages are flushed at every model-call boundary and tool results the moment they return; the `persisted_message_ids` watermark makes each message land exactly once, so persistence no longer depends on a compression firing.
- **Tool-result volume governance** — `ToolResultEvictionMiddleware` offloads results over 20 000 chars to `SESSIONS_DIR/<session_id>/evicted/` before they enter state (only a head+tail preview stays in context); `clear_session()` removes the whole session directory, evictions included. The P1-2 overflow tail clip only stubs trailing `ToolMessage` contents via `model_copy` (identity and pairing intact) — no data is lost, because every result is already persisted and offloaded text stays on disk: `message_search` recalls it, `read_file` re-reads the eviction file.
- **`agent/middlewares/summarization/compaction_lock.py`** — SQLite compaction lock (TTL self-healing, sync + async acquire, fail-open on timeout), wrapping both `_apply_compression` and `_aapply_compression`.
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
    tests/context_engine/embeddings/test_semantic_search.py \
    tests/agent/tools/test_message_search_checkpoint_fallback.py -q
```

## Eval

```bash
uv run python evals/evals.py session_memory
```

Scores the live subsystem under the eval sandbox — 6 checks covering cooldown restart survival, lock mutual exclusion, checkpoint restore, idempotent replay, context projection and real-embed semantic ranking. See `evals/session_memory/suite.py`.
