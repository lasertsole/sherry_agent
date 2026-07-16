# MesMemory — Session Message Memory System

[**中文文档**](README.zh.md) | **English**

> **MesMemory** is the short-term conversation memory engine for the EMA AI Agent, responsible for message persistence, history retrieval, full-text search.

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
2. **History Retrieval** — Fetch recent N turns, paginated history, or a specific turn range as formatted context
3. **Full-Text Search** — FTS5-based dialogue search with Chinese support (trigram) and context previews

---

## Architecture

```
┌────────────────────────────────────────────────────┐
│                   context_engine                     │
├───────────────────┬────────────────────────────────┤
│    store/         │          core.py                │
│   (Data Layer)    │      (Business Logic)           │
├───────────────────┼────────────────────────────────┤
│ • db.py           │ • retrieve_history_by_last_n   │
│   - SQLite conn    │   _prompt() → formatted        │
│   - Migrations     │   conversation string          │
│ • core.py         │ • search_messages() → FTS5     │
│   - CRUD ops       │   search + context             │
│   - Message writes │ • _sanitize_fts5_query()     │
│   - Turn queries   │   query sanitization           │
│   - Paginated      │ • _decode_content()          │
│     history        │   JSON content decoding        │
└───────────────────┴────────────────────────────────┘
```

### Store Layer (`store/`)

| File | Responsibility |
|------|---------------|
| `store/db.py` | SQLite connection management, WAL mode, auto-migration (tables, indexes, FTS5 triggers) |
| `store/core.py` | Message CRUD: `add_messages`, `get_messages_by_lastest_n_turns`, `get_turns_by_turn_num_scope`, `get_history_by_page`, `get_max_turn_num` |

### Business Layer (`core.py`)

| Function | Responsibility |
|----------|---------------|
| `retrieve_history_by_last_n_prompt(session_id, n)` | Get the last N turns and format as prompt context |
| `search_messages(query, session_id, ...)` | FTS5 full-text search with Chinese trigram support and context expansion |
| `_sanitize_fts5_query(query)` | Sanitize user input for safe FTS5 MATCH queries (internal) |
| `_decode_content(content)` | Reverse JSON-encoded message content (internal) |

### Package Exports (`__init__.py`)

```python
# context_engine/__init__.py
from .store import *                                              # get_db, add_messages, get_messages_by_lastest_n_turns, get_turns_by_turn_num_scope, get_history_by_page
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## Data Model

### Database Schema

```sql
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
from context_engine.mes_memory.store import add_messages

# Write a dialogue turn (auto-increments turn_num)
await add_messages("session_001", [user_msg, ai_msg])
```

- Messages of all three roles (human/ai/tool) are persisted
- Human messages from compression (identified by `lc_source == "summarization"`) are filtered out
- Each message carries a `YYYYMMDDHHmmss` timestamp
- Content is JSON-encoded with `\x00json:` prefix for structured data

---

### 2. History Retrieval

```python
from context_engine import retrieve_history_by_last_n_prompt

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
from context_engine.mes_memory.store import get_turns_by_turn_num_scope

# Get 5 turns before and after target_turn_num
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

Paginated history retrieval:

```python
from context_engine.mes_memory.store import get_history_by_page

# Get page 1 with 10 turns per page
rows = get_history_by_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

---

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
    print(r["snippet"])        # Highlighted snippet
    print(r["context"])        # 1 message of context before and after
```

**Search Features:**

- **Dual FTS5 Tables**: `messages_fts` (default unicode61 tokenizer) and `messages_fts_trigram` (trigram tokenizer, supports Chinese)
- **Auto-Routing**: Detects Chinese queries (3+ CJK characters per token) → trigram path; otherwise → default FTS5
- **Graceful Degradation**: Short Chinese queries (<3 CJK characters per token) fall back to LIKE search
- **Per-token CJK Check**: Multi-term queries like "广西 OR 桂林 OR 漓江" are checked per token — if any CJK token has <3 chars, the whole query routes to LIKE
- **Query Sanitization**: Automatically handles FTS5 special characters, quote balancing, boolean operator cleanup, hyphenated/dotted term quoting
- **Context Expansion**: Each result includes 1 message of context before and after
- **Multimodal-Friendly**: Non-text content (e.g., images) is shown as `[multimodal content]`
- **Token Efficiency**: Results omit the full `content` field (snippet + context only)
- **Thread Safety**: All DB operations are protected by a threading lock

---

## API Reference

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

### `add_messages(session_id, messages)`
(Store layer) Write messages to the database.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage list |

---

### `get_messages_by_lastest_n_turns(session_id, last_n=5)`
Fetch raw message records for the last N turns from the store layer.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `last_n` | `int` | Number of turns (default: 5) |

**Returns:** `list[dict]` — Each record contains all message fields

---

### `get_turns_by_turn_num_scope(session_id, target_turn_num, half_scope=5)`
Get messages within a turn range around a target turn number.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `target_turn_num` | `int` | Target turn number |
| `half_scope` | `int` | Number of turns on each side (default: 5) |

**Returns:** `list[dict]` — Each record contains all message fields with decoded JSON

---

### `get_history_by_page(session_id, min_turn_num=1, turn_page_size=10, turn_page_num=1)`
Fetch paginated history messages.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |
| `min_turn_num` | `int` | Minimum turn number (≥1, default: 1) |
| `turn_page_size` | `int` | Turns per page (≥1, default: 10) |
| `turn_page_num` | `int` | Page number (≥1, default: 1) |

**Returns:** `list[dict]` — Each record contains all message fields with decoded JSON

---

### `get_max_turn_num(session_id)`
Get the maximum turn number for a session.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Session ID |

**Returns:** `int` — Maximum turn number, or 0 if no messages exist

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

### Q4: How does the per-token CJK routing work?

For CJK queries, the system checks each non-operator token individually. If any CJK token has fewer than 3 CJK characters, trigram FTS5 cannot match it (it requires ≥3 CJK chars per token), so the entire query falls back to LIKE search. This handles cases like `"广西 OR 桂林 OR 漓江"` where each term is only 2 CJK chars.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Database** | SQLite 3 + WAL mode |
| **Full-Text Search** | FTS5 + Trigram tokenizer |
| **Framework** | LangChain BaseMessage |
| **Validation** | Pydantic `@validate_call` |
| **Storage Path** | `store/mes_memory/mes_memory.db` |

---

## License

This project follows the open-source license of the EMA AI Agent.

---

**Last updated:** 2026-07-09
