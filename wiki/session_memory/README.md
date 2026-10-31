# Session Memory Architecture (SESSION Plan)

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

Twelve of the SESSION memory plan's 13 capabilities (borrowed from opencode-dev, oh-my-openagent, hermes-agent and openclaw) remain implemented — the pre-compression memory flush was retired because the compression-time memory review subsumes it — plus the long-running-task orchestrations. Design rule: every capability extends the existing infrastructure — session continuity, state registers, MesMemory schema — never a parallel store.

> Status (2026-09-13): plan retired. This README is the reference.

## Implemented Capabilities

| Capability | Where |
|---|---|
| Compression cooldown persisted across restarts | `_COOLDOWN_PERSIST_KEYS` in `agent/middlewares/summarization/core.py` |
| SQLite compaction lock (TTL, fail-open) | `agent/middlewares/summarization/compaction_lock.py`, current schema |
| One-line tool-output summaries | `pub/func/message/tool_output_prune.py` |
| Compaction checkpoints + restore | current schema, `restore_compaction_checkpoint` |
| Idempotent message persistence | current schema (`idempotency_key` + partial unique index) |
| `context_eligible` history projection | current schema; retrieval filters ineligible rows by default |
| Message tree + zero-copy forking | current schema (`parent_message_id`, `session_leafs`) |
| Append-only event log + projector | current schema, `context_engine/events/` |
| Context epoch snapshots | current schema (`context_epoch` table), `ContextEpoch` |
| steer/queue dual delivery | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| Vector semantic search | current schema, `context_engine/embeddings/`, `message_search --semantic` |
| TaskFlow orchestration | `wiki/long-running-tasks/` |

## Memory Files

The three single-file memory stores are loaded by `MemoryStore.load_from_disk()` (`agent/tools/memory.py`) and captured as a frozen system-prompt snapshot. FACTS.md is one global file (`workspace/memory/FACTS.md`), loaded with MEMORY.md and USER.md; it records only SYSTEM-LEVEL / CROSS-PROJECT pitfalls — things true in ANY project. Project-specific facts go to MEMORY.md or the project's AGENTS.md, and the nudges enforce that routing (including migrating a misplaced project fact out of FACTS into MEMORY). There is no per-project FACTS file; a legacy `<project>/.sherry/memory/FACTS.md` is ignored. FACTS.md is maintained by the memory-review and plan-extraction nudges. The snapshot stays frozen for the session except when the session's model or thinking control changes: the provider's prefix cache is already cold after such a switch, so the next turn's `before_model` hook reloads the snapshot and invalidates the cached system prompt, making mid-session memory writes visible on that turn's first model call.

| File | Purpose | Limit | Maintainer |
|---|---|---|---|
| `workspace/memory/MEMORY.md` | Agent-side notes: environment facts, project conventions, tool quirks | 2,200 chars (`MEMORY_TOOL["memory_char_limit"]`) | `memory` tool (`memory` target: memory review nudge) |
| `workspace/memory/USER.md` | User profile: preferences, communication style, expectations | 1,375 chars (`MEMORY_TOOL["user_char_limit"]`) | `memory` tool (`user` target: memory review nudge) |
| `workspace/memory/FACTS.md` | SYSTEM-LEVEL / CROSS-PROJECT pitfalls and conventions; injected into every system prompt | 1,375 chars (`MEMORY_TOOL["facts_char_limit"]`), oldest-first rollover on overflow | `memory` tool (`facts` target: memory review / plan extraction nudges) |

## MesMemory Schema

| Schema object | Purpose |
|---|---|
| `compression_locks` | per-session compaction lock (PRIMARY KEY = mutex) |
| `messages.idempotency_key` + partial unique index | crash-retry dedup |
| `messages.context_eligible` | context projection flag (default 1) |
| `message_embeddings` | vector index for semantic search |
| `messages.parent_message_id` + `session_leafs` | message tree |
| `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` | compaction restore points |
| `events` | append-only log, gapless per-session `seq` |
| `context_epoch` | system-context baseline / snapshot |

`build_schema_v1` creates this complete schema in one call; there is no versioned migration chain.

## Key Components

- **`context_engine/events/`** — append-only event log with gapless per-session sequences (`types.py`, `store.py`), and an `EventProjector` mapping checkpoint events onto the checkpoint read model.
- **`context_engine/embeddings/`** — vector semantic search: lazy embed backend (project embed model, overridable), idempotent LEFT-JOIN-driven indexer, cosine ranking; exposed by the `message_search` tool (`semantic: true`).
- **`agent/tools/message_search.py`** — two-stage recall: FTS5 over the persisted `messages` table first; when there is no hit it falls back to the session's newest checkpoint (`SRC_DIR/checkpoints/sqlite.db`, `state["messages"]`), keyword-matching not-yet-persisted turns newest-first (bounded by `_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars`); fallback hits are tagged `source="checkpoint"`. Since persistence runs at every model boundary and on every tool return, this fallback only matters in the narrow window where the checkpoint runs ahead of the store — HITL denial messages waiting for the next model boundary to persist them (tool results are already written the moment they return).
- **`agent/middlewares/message_persistence/`** — write-once session persistence at two timings: new human/ai messages are flushed at every model-call boundary and tool results the moment they return; the `persisted_message_ids` watermark makes each message land exactly once, so persistence does not depend on a compression firing.
- **Context volume governance** — `ContextEvictionMiddleware` offloads tool results over 20 000 chars to `SESSIONS_DIR/<session_id>/evicted/` before they enter state (only a head+tail preview stays in context). Oversized human messages (> 200 000 chars) are written to the same directory and tagged with `lc_evicted_to`, but only their model view is truncated — state and MesMemory keep the full text. `clear_session()` removes the whole session directory, evictions included. The P1-2 overflow tail clip only stubs trailing `ToolMessage` contents via `model_copy` (identity and pairing intact) — no data is lost, because every result is already persisted and offloaded text stays on disk: `message_search` recalls it, `read_file` re-reads the eviction file.
- **Terminal stdout never rides a pipe** — the `terminal` tool hands the child process a FILE for stdout (stderr is merged into it, as before) and only then decides: under `TOOLS_TIMEOUTS["fd_write_threshold_bytes"]` (100 000 bytes) the temp file is read whole and removed; at or above it the file is renamed to `evicted/terminal-<sha256(first 64 KiB)[:16]>.log` and a head/tail preview labelled with the byte total (`stat().st_size` — the file is never walked to count lines) comes back instead. The preview itself is byte-bounded, so even a newline-free 500 MB output costs a few kilobytes; a timed-out or cancelled command deletes its own temp file, and leftovers from a hard kill are swept by the shared `.sherry-tmp-` / `.log` contract in `atomic_write.py`.
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
