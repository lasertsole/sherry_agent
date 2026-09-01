# MesMemory — Session Message Memory System

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **MesMemory** is the short-term conversation memory engine of the EMA AI Agent (the `context_engine` package): SQLite persistence of session messages, history retrieval, and FTS5 full-text search. The package also ships the **Curator** subpackage for background skill maintenance — see [Curator](#curator-skill-maintenance-subpackage).

---

## Table of Contents

- [Overview](#overview)
- [Package Layout](#package-layout)
- [Data Model](#data-model)
- [Core Features](#core-features)
- [Integration](#integration)
- [Curator (Skill Maintenance Subpackage)](#curator-skill-maintenance-subpackage)
- [API Reference](#api-reference)
- [FAQ](#faq)
- [Tech Stack](#tech-stack)

---

## Overview

### Design Position

MesMemory is a **short-term, per-session message store**. It is deliberately simple: all storage and retrieval is SQL/FTS5-based — there are no vector embeddings, no graph algorithms, and no rerankers in this package.

| | MesMemory |
|---|-----------|
| Scope | Raw `human` / `ai` / `tool` messages of each session |
| Storage | One shared SQLite database (`src/store/mes_memory/mes_memory.db`) |
| Retrieval | Last-N turns, turn-range queries, paginated history, FTS5 full-text search |
| Writes | `await add_messages(...)` — one call persists one turn |

Long-term maintenance of agent-created skills (lifecycle transitions, consolidation, pruning) is handled by the separate [Curator](#curator-skill-maintenance-subpackage) subpackage inside `context_engine/` — it does **not** touch message data.

### Core Capabilities

1. **Message Persistence** — Write the `human`/`ai`/`tool` messages of each dialogue turn to SQLite
2. **History Retrieval** — Fetch the last N turns, a turn range, or a paginated page of history
3. **Full-Text Search** — FTS5-based dialogue search with a trigram path for CJK queries, a LIKE fallback, and context previews
4. **Session Management** — List top-level sessions with derived titles; delete all messages of a session

---

## Package Layout

```
context_engine/
├── __init__.py          # Package exports (re-exports store + core APIs)
├── core.py              # Business layer: history prompt formatting, FTS5 search
├── store/
│   ├── __init__.py      # Store-layer exports
│   ├── db.py            # SQLite connection, WAL mode, versioned migrations (tables, indexes, FTS5 triggers)
│   └── core.py          # Message CRUD: add/query/delete + session listing
└── curator/             # Background skill maintenance orchestrator (has its own README)
```

```
┌──────────────────────────────────────────────────────┐
│                    context_engine                    │
├──────────────────────┬───────────────────────────────┤
│   store/  (Data)     │     core.py  (Business)       │
├──────────────────────┼───────────────────────────────┤
│ • db.py              │ • retrieve_history_by_last_   │
│   - SQLite conn      │   n_prompt() → formatted      │
│   - WAL + migrations │   conversation string         │
│ • core.py            │ • search_messages() → FTS5 /  │
│   - add_messages     │   trigram / LIKE routing      │
│   - turn queries     │ • _sanitize_fts5_query()      │
│   - paged history    │   query sanitization          │
│   - session listing  │ • _decode_content()           │
│                      │   JSON content decoding       │
└──────────────────────┴───────────────────────────────┘
```

### Package Exports (`__init__.py`)

```python
# context_engine/__init__.py
from .store import *   # get_db, add_messages, get_messages_by_lastest_n_turns,
                       # get_turns_by_turn_num_scope, get_history_by_turn_page,
                       # get_session_ids, delete_messages_by_session
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## Data Model

### Database Schema

```sql
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,   -- Turn sequence number (one add_messages call = one turn)
    session_id    TEXT NOT NULL,      -- Session ID
    role          TEXT NOT NULL,      -- human / ai / tool
    content       TEXT,               -- Message content (json.dumps, ensure_ascii=False)
    tool_call_id  TEXT,               -- Tool call ID (tool messages)
    tool_calls    TEXT,               -- Tool call details, JSON (AI messages)
    tool_status   TEXT,               -- Tool execution status (default "success")
    tool_name     TEXT,               -- Tool name
    timestamp     TEXT NOT NULL,      -- Timestamp YYYYMMDDHHmmss (shared by the whole batch)
    finish_reason TEXT,               -- AI response finish reason
    reasoning     TEXT,               -- Chain-of-thought (additional_kwargs["reasoning_content"])
    reasoning_content TEXT,           -- Reasoning process
    images        TEXT,               -- JSON list of image paths (human multimodal input)
    audios        TEXT,               -- JSON list of audio paths/references
    videos        TEXT,               -- JSON list of video paths/references
    model_name    TEXT,               -- AI messages: model that produced the response
    input_tokens  INTEGER,            -- AI messages: usage_metadata input tokens
    output_tokens INTEGER,            -- AI messages: usage_metadata output tokens
    origin        TEXT                -- Message origin tag ("subagent_completion" for completion carriers; NULL otherwise)
);
```

**Indexes:**

- `idx_messages_timestamp` — `(session_id, timestamp)`
- `idx_messages_turn_num` — `(session_id, turn_num)`

**FTS5 tables** (both index the concatenation of `content`, `tool_name` and `tool_calls`):

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**FTS5 Triggers:** each FTS table has `AFTER INSERT` / `AFTER UPDATE` / `AFTER DELETE` triggers on `messages` that keep the index in sync automatically. Deleting rows (e.g. `delete_messages_by_session`) therefore needs no separate FTS cleanup.

**Migrations:** schema creation is versioned in a `_migrations` table. The steps, in order:
`build_messages_tb` → `build_messages_fts_tb` → `build_messages_fts_trigram_tb` → `add_images_column` → `add_audio_video_columns` → `add_model_token_columns` → `add_origin_column`.

---

## Core Features

### 1. Message Persistence

```python
from context_engine.store import add_messages

# Persist one dialogue turn (turn_num auto-increments per call)
await add_messages("session_001", [user_msg, ai_msg])
```

- One `add_messages` call = one turn: all messages in the batch share the same `turn_num` and the same `YYYYMMDDHHmmss` timestamp
- `human` messages produced by summarization (identified by `additional_kwargs["lc_source"] == "summarization"`) are filtered out
- `ai` messages persist `tool_calls` (JSON), chain-of-thought from `additional_kwargs["reasoning_content"]` (stored in the `reasoning` column), plus `model_name` / `input_tokens` / `output_tokens` from response & usage metadata (all optional, `None` when absent)
- `human` messages persist multimodal file references from `additional_kwargs` into the `images` / `audios` / `videos` columns (JSON lists, `None` when empty)
- `tool` messages persist `tool_call_id`, `tool_name`, and `tool_status` (defaults to `"success"`)
- A `human` message whose metadata sets `internal: true` and `provenance: "subagent_completion"` (the steering-queue completion carrier) is persisted with `origin = 'subagent_completion'`; every other row keeps `origin = NULL` (never an empty string, never JSON)

### 2. History Retrieval

```python
from context_engine import retrieve_history_by_last_n_prompt

# Get the last 5 turns, formatted as a prompt string
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**Output format** (verbatim from `core.py`; per-turn bodies contain no timestamps):

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
user: User message

agent: AI response
</turn>

...

===== The above is the content of the last 5 turns =====

```

For `human` messages whose content is a multimodal list, only the first `{"type": "text"}` part is used.

Turn-range queries are also supported:

```python
from context_engine.store import get_turns_by_turn_num_scope

# Get 5 turns before and after target_turn_num
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

Paginated history retrieval (page 1 is the most recent page):

```python
from context_engine.store import get_history_by_turn_page

# Get page 1 with 10 turns per page
rows = get_history_by_turn_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

Both turn-range and paged queries return rows newest turn first, with the JSON-encoded `content`, `tool_calls`, `images`, `audios` and `videos` columns decoded back into Python objects.

### 3. Full-Text Search

```python
from context_engine import search_messages

# Search for messages containing "Docker", with context preview
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # Highlighted snippet (markers: >>> match <<<)
    print(r["context"])        # Up to 3 entries: previous message, match, next message
```

**Search Features:**

- **Dual FTS5 Tables**: `messages_fts` (default unicode61 tokenizer) and `messages_fts_trigram` (trigram tokenizer, supports CJK substring matching)
- **Auto-Routing**: non-CJK queries go to `messages_fts`; CJK queries with ≥3 CJK characters in total and no token shorter than 3 CJK characters go to the trigram table; everything else falls back to LIKE
- **Per-token CJK Check**: multi-term queries like `广西 OR 桂林 OR 漓江` are checked per token — if any CJK token has <3 CJK characters, the whole query routes to LIKE (trigram requires ≥3 CJK characters per token)
- **LIKE Fallback**: one LIKE condition per non-operator token against `content`, `tool_name` and `tool_calls` (with `ESCAPE '\'`), ordered by `timestamp DESC`; the snippet is a 120-character window positioned around the first token occurrence
- **Query Sanitization** (`_sanitize_fts5_query`): preserves paired quoted phrases, strips unmatched FTS5-special characters, collapses repeated `*`, removes dangling `AND`/`OR`/`NOT`, and quotes hyphenated/dotted/underscored terms (e.g. `my-app.config.ts`) so FTS5 treats them as phrases
- **Trigram Token Quoting**: on the trigram path each non-operator token is wrapped in double quotes while boolean operators (`AND`, `OR`, `NOT`) are preserved
- **Context Expansion**: each match gets a context list of up to three entries — the preceding message, the match itself, and the following message (ordered by `timestamp`, then `id`) — each rendered as `{"role": ..., "content": preview}` with the preview truncated to 200 characters; multimodal list content renders its text parts or `[multimodal content]`
- **Token Efficiency**: the full `content` field is removed from results (snippet + context only)
- **Error Tolerance**: empty/unsanitizable queries return `[]`; FTS5 `sqlite3.OperationalError` on MATCH is swallowed and returns `[]`
- **Thread Safety**: all DB access is guarded by a module-level `threading.Lock`
- **Ordering**: FTS5 paths order by relevance (`ORDER BY rank`); the LIKE path orders by `timestamp DESC`

---

## Integration

Verified consumers of the `context_engine` package:

| Entry point | Imports | Purpose |
|-------------|---------|---------|
| `agent/middlewares/context_engine/core.py` → `ContextEngineHook` | `add_messages` | Agent middleware (registered in the main agent in `agent/core.py`). On `aafter_agent` it slices the last turn (`slice_last_turn`), sanitizes it (`sanitize_tool_use_result_pairing`) and persists it via `add_messages()`; it also injects the system prompt (`wrap_model_call`/`awrap_model_call`) and runs memory/skill nudge counters (threshold 10) & nudge sub-agents. See `agent/middlewares/README.md`. |
| `agent/tools/message_search.py` → `message_search` tool | `get_db`, `search_messages`, `get_turns_by_turn_num_scope` | Cross-session recall tool: FTS5 search (limit 50) → per-match turn-range fetch → LLM session summaries; with no query it returns recent-session metadata instead |
| `server/service/messages.py` | `get_session_ids`, `get_history_by_turn_page`, and `reset_idle_for_seconds` (from `context_engine.curator`) | Client-facing session list (top-level sessions with derived titles), paginated history, and curator idle-time reset on every user turn |
| `server/DAO/messages.py` | `delete_messages_by_session` | "Clear session" operation |
| `server/trigger/http/stats.py` | `get_db` (from `context_engine.store.db`) | Usage statistics over the messages table |
| `server/__main__.py` | `import context_engine.curator` | Importing the curator package starts its background daemon thread |

---

## Curator (Skill Maintenance Subpackage)

`context_engine/curator/` is a **background skill maintenance orchestrator** — it has nothing to do with message storage. Summary of verified behavior:

- **Scope**: only agent-created skills under `skills/auto/`; never touches built-in skills
- **Trigger**: importing `context_engine.curator` starts a daemon thread (`curator-timer`) that calls `maybe_run_curator()` every 3600 s; a run executes only when `should_run_now()` is true (enabled, not paused, `interval_hours` elapsed) and the agent has been idle long enough (`min_idle_hours`). `reset_idle_for_seconds()` is called on every user turn (`server/service/messages.py`)
- **Lifecycle**: `active → stale` after `stale_after_days` (default 30) without activity; skills past `archive_after_days` (default 90) are removed from disk; never-used skills inside the stale window are reactivated. Pinned skills bypass all transitions
- **LLM consolidation** (opt-in via `curator.yaml`, `consolidate: false` by default): merges overlapping narrow skills into umbrella skills generated by an LLM
- **State & reports**: run state in `skills/.curator_state`; reports under `logs/curator/{timestamp}/` (`run.json` + `REPORT.md`)

Public API includes `run_curator_review(on_summary=None, dry_run=False, consolidate=None)`, `maybe_run_curator(*, idle_for_seconds=None, on_summary=None)`, `reset_idle_for_seconds()`, `pin_skill(name)`, `unpin_skill(name)`, `delete_skill(name, absorbed_into="")`, `apply_automatic_transitions(now=None)`, and `should_run_now(now=None)`.

▶️ Full details: [curator/README.md](curator/README.md) · [中文](curator/README.zh.md) · [한국어](curator/README.ko.md) · [日本語](curator/README.ja.md)

---

## API Reference

Signatures below are copied from the source; import paths are given per layer.

### Business layer (`context_engine.core`, re-exported at package level)

#### `retrieve_history_by_last_n_prompt(session_id: str, n: int = 5) -> str`
Format the last `n` turns as a prompt string (see output format above).

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `n` | `int` | Number of turns (default: 5) |

**Returns:** `str` — Formatted conversation history

---

#### `search_messages(query: str, session_id: str, role_filter: list[str] = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]`
Full-text search messages.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str` | Search query (empty → `[]`) |
| `session_id` | `str` | Session ID |
| `role_filter` | `list[str]` | Role filter (e.g. `["human", "ai"]`; default `None`) |
| `limit` | `int` | Max results (default: 20) |
| `offset` | `int` | Offset (default: 0) |

**Returns:** `list[dict[str, Any]]` — Each result contains `id`, `session_id`, `turn_num`, `role`, `snippet`, `timestamp`, `tool_name`, `context` (the full `content` field is removed)

---

#### `_sanitize_fts5_query(query: str) -> str` (internal)
Sanitize user input for safe FTS5 MATCH queries.

#### `_decode_content(content: Any) -> Any` (internal)
Decode message content strings that carry the `\x00json:` prefix; returns other values unchanged.

---

### Store layer (`context_engine.store`)

#### `async add_messages(session_id: str, messages: list[BaseMessage]) -> None`
Persist a batch of LangChain messages as one new turn.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `messages` | `list[BaseMessage]` | LangChain `BaseMessage` list (`human` / `ai` / `tool`) |

---

#### `get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5) -> list[dict]`
Fetch the message rows of the last `last_n` turns (delegates to `get_history_by_turn_page` with page 1).

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `last_n` | `int` | Number of turns (default: 5) |

**Returns:** `list[dict]` — Message rows, newest turn first, JSON columns decoded

---

#### `get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]`
Get messages within a turn range around a target turn number (clamped to `[1, max_turn_num]`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `target_turn_num` | `int` | Target turn number |
| `half_scope` | `int` | Number of turns on each side (default: 5) |

**Returns:** `list[dict]` — Message rows, newest turn first, JSON columns decoded

---

#### `get_history_by_turn_page(session_id: str, min_turn_num: Annotated[int, Field(ge=1)] = 1, turn_page_size: Annotated[int, Field(ge=1)] = 10, turn_page_num: Annotated[int, Field(ge=1)] = 1) -> list[dict]`
Fetch a page of history, paginated by turn number from the newest turn backward (decorated with `@validate_call`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `min_turn_num` | `int` | Inclusive lower bound for `turn_num` (≥1, default: 1) |
| `turn_page_size` | `int` | Turns per page (≥1, default: 10) |
| `turn_page_num` | `int` | 1-based page index from the newest turn backward (≥1, default: 1) |

**Returns:** `list[dict]` — Message rows, newest turn first, JSON columns decoded

---

#### `get_max_turn_num(session_id: str) -> int`
Maximum `turn_num` of a session; `0` when the session has no messages. Defined in `context_engine/store/core.py` (not re-exported by `context_engine.store`).

---

#### `delete_messages_by_session(session_id: str) -> int`
Delete all messages of a session. FTS5 indexes are cleaned up automatically by triggers.

**Returns:** `int` — Number of rows deleted

---

#### `get_session_ids() -> list[dict]`
Enumerate distinct top-level sessions (subagent sessions containing `:subagent:` are excluded), newest activity first.

**Returns:** `list[dict]` — Each item: `{"session_id": str, "last_time": str, "title": str}` where `last_time` is the newest `YYYYMMDDHHmmss` timestamp and `title` is derived from the latest `human` message (may be `""`)

The title query only considers rows with `origin IS NULL`; a session whose `human` rows are all `subagent_completion` carriers therefore yields an empty title, and clients render a placeholder.

---

#### `get_db()` (`context_engine.store.db`)
Return the shared `sqlite3.Connection` (created on first call with `check_same_thread=False`, `timeout=1.0`, `isolation_level=None`, `row_factory=sqlite3.Row`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`).

---

## FAQ

### Q1: What is the relationship between MesMemory and the Curator?

They live in the same package but are unrelated at runtime: MesMemory stores and retrieves **raw session messages** (short-term memory). The Curator maintains **agent-created skills** under `skills/auto/` (lifecycle transitions, consolidation, pruning). The Curator never reads or writes the `messages` table.

---

### Q2: Why two FTS5 tables?

`messages_fts` uses the default unicode61 tokenizer, suitable for English-style token matching. `messages_fts_trigram` uses the trigram tokenizer, which splits text into 3-gram substrings, enabling CJK substring matching (the unicode61 tokenizer would split CJK text into single characters and produce false positives). The router picks the table based on the query's CJK content and token lengths.

---

### Q3: What's the difference between `snippet` and `content` in search results?

On the FTS5 paths, `snippet` is FTS5's excerpt with `>>>` / `<<<` highlight markers around the match (40-token window). On the LIKE path, `snippet` is a 120-character slice of `content` positioned around the first token occurrence (no markers). The full `content` field is popped from every result to save tokens; use `get_messages_by_lastest_n_turns` / `get_history_by_turn_page` when you need full content.

---

### Q4: How does the per-token CJK routing work?

For CJK queries, every non-operator token is checked individually. If any CJK token has fewer than 3 CJK characters, trigram FTS5 cannot match it (it requires ≥3 CJK characters per token), so the entire query falls back to LIKE search. This handles cases like `"广西 OR 桂林 OR 漓江"` where each term is only 2 CJK characters, even though the total CJK count is 6.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Database** | SQLite 3 — WAL mode, `foreign_keys=ON`, single shared connection (`check_same_thread=False`, `timeout=1.0`) |
| **Full-Text Search** | FTS5 (unicode61) + FTS5 (trigram tokenizer) |
| **Message Model** | LangChain `BaseMessage` |
| **Validation** | Pydantic `@validate_call` (on `get_history_by_turn_page`) |
| **Concurrency** | `threading.Lock` around all DB access |
| **Storage Path** | `src/store/mes_memory/mes_memory.db` (`config.path.SRC_DIR / "store/mes_memory/mes_memory.db"`) |

---

## License

This project follows the open-source license of the EMA AI Agent.

---

**Last updated:** 2026-09-02
