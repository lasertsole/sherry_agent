# MesMemory — Session Message Memory System

[**中文文档**](README.zh.md) | **English**

> **MesMemory** is the short-term conversation memory engine for the EMA AI Agent, responsible for message persistence, history retrieval, full-text search, and user preference extraction.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Data Model](#data-model)
- [Core Features](#core-features)
- [API Reference](#api-reference)
- [FAQ](#faq)

---

## Overview

### Design Position

MesMemory complements [Skill Memory](../skill_memory/README.md):

| Skill Memory | MesMemory |
|-------------|-----------|
| Long-term knowledge graph (TASK/SKILL/EVENT) | Short-term session message storage |
| Structured triples, cross-session reuse | Raw message sequences, per-session isolated |
| Graph community + PageRank recall | FTS5 full-text search + turn-range queries |
| Async background extraction | Sync writes, immediate persistence |

### Core Capabilities

1. **Message Persistence** — Write human/ai/tool messages from each dialogue turn to SQLite
2. **History Retrieval** — Fetch recent N turns or a specific turn range as formatted context
3. **Full-Text Search** — FTS5-based dialogue search with Chinese support (trigram) and context previews
4. **Memory Nudging** — Periodically extract user preferences from conversation and write them into the long-term memory store

---

## Architecture

```
┌────────────────────────────────────────────────────┐
│                   MesMemory Core                    │
├───────────────────┬────────────────────────────────┤
│    store/         │          core.py                │
│   (Data Layer)    │      (Business Logic)           │
├───────────────────┼────────────────────────────────┤
│ • db.py           │ • retrieve_history_by_last_n   │
│   - SQLite conn    │   _prompt() → formatted        │
│   - Migrations     │   conversation string          │
│ • core.py         │ • search_messages() → FTS5     │
│   - CRUD ops       │   search + context             │
│   - Message writes │ • nudge_memory() →          │
│   - Turn queries   │   trigger preference extr.     │
└───────────────────┴────────────────────────────────┘
```

### Store Layer (`store/`)

| File | Responsibility |
|------|---------------|
| `store/db.py` | SQLite connection management, WAL mode, auto-migration (tables, indexes, FTS5 triggers) |
| `store/core.py` | Message CRUD: `add_messages`, `get_messages_by_lastest_n_turns`, `get_turns_by_turn_num_scope`, `update_session` |

### Business Layer (`core.py`)

| Function | Responsibility |
|----------|---------------|
| `retrieve_history_by_last_n_prompt(session_id, n)` | Get the last N turns and format as prompt context |
| `search_messages(query, session_id, ...)` | FTS5 full-text search with Chinese trigram support and context expansion |
| `append_messages(session_id, messages)` | Write messages and trigger nudge check |
| `nudge_memory(session_id, ...)` | Check if the nudge threshold is reached and trigger preference extraction |

---

## Data Model

### Database Schema

```sql
-- Sessions table
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    nudge_turn_num INTEGER NOT NULL DEFAULT 0
);

-- Messages table
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,       -- Turn sequence number
    session_id    TEXT NOT NULL,           -- Session ID
    role          TEXT NOT NULL,           -- human / ai / tool
    content       TEXT,                   -- Message content (JSON-encoded)
    tool_call_id  TEXT,                   -- Tool call ID
    tool_calls    TEXT,                   -- Tool call details (JSON)
    tool_status   TEXT,                   -- Tool execution status
    tool_name     TEXT,                   -- Tool name
    timestamp     TEXT NOT NULL,          -- Timestamp (YYYYMMDDHHmmss)
    finish_reason TEXT,                   -- AI response finish reason
    reasoning     TEXT,                   -- Reasoning content
    reasoning_content TEXT                -- Reasoning process
);

-- FTS5 full-text search (English-first)
CREATE VIRTUAL TABLE messages_fts USING fts5(content);

-- FTS5 Chinese trigram search
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**Indexes:**
- `idx_messages_timestamp` — `(session_id, timestamp)` for fast time-range queries
- `idx_messages_turn_num` — `(session_id, turn_num)` for turn-based queries

**FTS5 Triggers:** Automatically sync FTS5 indexes on INSERT/UPDATE/DELETE of the `messages` table. Indexed fields include `content`, `tool_name`, and `tool_calls`.

---

## Core Features

### 1. Message Persistence

```python
from context_engine.mes_memory import append_messages

# Write a dialogue turn (auto-increments turn_num)
await append_messages("session_001", [user_msg, ai_msg])
```

- Messages of all three roles (human/ai/tool) are persisted
- Human messages from compression (identified by `lc_source == "summarization"`) are filtered out
- Each message carries a `YYYYMMDDHHmmss` timestamp

---

### 2. History Retrieval

```python
from context_engine.mes_memory import retrieve_history_by_last_n_prompt

# Get last 5 turns, formatted as prompt string
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**Output format:**

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
User: User message

Assistant: AI response
</turn>

...

===== The above is the content of the last 5 turns =====
```

Turn-range queries are also supported:

```python
from context_engine.store import get_turns_by_turn_num_scope

# Get 5 turns before and after target_turn_num
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

---

### 3. Full-Text Search

```python
from context_engine.mes_memory import search_messages

# Search for messages containing "Docker", with context preview
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # Highlighted snippet
    print(r["context"])        # 1 message of context before and after
```

**Search Features:**

- **Dual FTS5 Tables**: `messages_fts` (default unicode61 tokenizer) and `messages_fts_trigram` (trigram tokenizer, supports Chinese)
- **Auto-Routing**: Detects Chinese queries (3+ CJK characters) → trigram path; otherwise → default FTS5
- **Graceful Degradation**: Short Chinese queries (<3 CJK characters) fall back to LIKE search
- **Query Sanitization**: Automatically handles FTS5 special characters, quote balancing, and boolean operator cleanup
- **Context Expansion**: Each result includes 1 message of context before and after
- **Multimodal-Friendly**: Non-text content (e.g., images) is shown as `[multimodal content]`
- **Token Efficiency**: Results omit the full `content` field (snippet + context only)

---

### 4. Memory Nudging (Preference Extraction)

```python
from context_engine.mes_memory import nudge_memory, append_messages

# Auto-check nudge on message write
await append_messages("session_001", messages, nudge_turn=10)

# Or trigger manually
await nudge_memory("session_001", nudge_turn=10, skip_last_turn=False)
```

**Workflow:**

```
Write messages → Check turn diff from last nudge
    ↓ diff < nudge_turn (default 10)
    Skip
    ↓ diff ≥ nudge_turn
    1. Fetch all messages since last nudge
    2. Filter out tool messages, keep human/ai only
    3. Merge by turn and role into BaseMessage list
    4. Load existing preferences from memory store
    5. Call extract_memory_agent (LLM)
    6. Agent writes/updates user preferences via memory tools
    7. Update nudge_turn_num in sessions table
```

**Features:**
- Incremental processing: only processes new conversations since the last nudge
- Deduplication: the agent is instructed not to add existing preferences
- Capacity management: auto-merges or removes old preferences when full
- Smart filtering: skips items the user manually updated via memory tools

---

## API Reference

### `append_messages(session_id, messages, nudge_turn=10)`
Write messages and trigger nudge check.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage list |
| `nudge_turn` | `int` | Nudge check interval (default: 10 turns) |

---

### `retrieve_history_by_last_n_prompt(session_id, n=5)`
Get the last N turns and format as a prompt string.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `n` | `int` | Number of turns (default: 5) |

**Returns:** `str` — Formatted conversation history

---

### `search_messages(query, session_id, role_filter=None, limit=20, offset=0)`
Full-text search messages.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str` | Search query |
| `session_id` | `str` | Session ID |
| `role_filter` | `list[str]` | Role filter (e.g., `["human", "ai"]`) |
| `limit` | `int` | Max results (default: 20) |
| `offset` | `int` | Offset (default: 0) |

**Returns:** `list[dict]` — Each result contains `id`, `session_id`, `turn_num`, `role`, `snippet`, `timestamp`, `tool_name`, `context`

---

### `nudge_memory(session_id, skip_last_turn=False, nudge_turn=10)`
Manually trigger user preference extraction.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `skip_last_turn` | `bool` | Whether to skip the latest turn |
| `nudge_turn` | `int` | Nudge check interval |

---

### `get_messages_by_lastest_n_turns(session_id, last_n=5)`
Fetch raw message records for the last N turns from the store layer.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `last_n` | `int` | Number of turns (default: 5) |

**Returns:** `list[dict]` — Each record contains all message fields

---

### `add_messages(session_id, messages)`
(Store layer) Write messages to the database without triggering nudge.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage list |

---

### `update_session(session_id, params)`
Update session attributes (e.g., nudge_turn_num).

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `params` | `dict` | Key-value pairs of fields to update (`session_id` is blocked) |

---

## FAQ

### Q1: What is the relationship between MesMemory and Skill Memory?

MesMemory handles **raw message storage and retrieval** (short-term memory). Skill Memory handles **knowledge extraction and graph construction** (long-term memory). MesMemory stores "what was said," while Skill Memory stores structured knowledge extracted from what was said.

---

### Q2: Why two FTS5 tables?

`messages_fts` uses the default unicode61 tokenizer, suitable for English and pinyin searches. `messages_fts_trigram` uses the trigram tokenizer, which splits text into 3-gram substrings, naturally supporting Chinese fuzzy matching and substring search. The system auto-selects based on the query language.

---

### Q3: What's the difference between `snippet` and `content` in search results?

`snippet` is a short FTS5-provided excerpt with highlight markers (~40 chars on each side), used for quick preview of match locations. `content` is the full message body, but it's omitted from search results to save tokens. If you need the full content, use `get_messages_by_lastest_n_turns` instead.

---

### Q4: What nudge_turn value should I use?

The default of 10 turns is recommended. Too frequent (e.g., 1–3 turns) results in an LLM call every turn for preference extraction, increasing cost. Too long (e.g., 30+ turns) may miss short-term user behavior changes. A range of 5–15 turns is advisable.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Database** | SQLite 3 + WAL mode |
| **Full-Text Search** | FTS5 + Trigram tokenizer |
| **Framework** | LangChain BaseMessage |
| **Storage Path** | `store/mes_memory/mes_memory.db` |

---

## License

This project follows the open-source license of the EMA AI Agent.

---

**Last updated:** 2026-05-30
