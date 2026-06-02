# Agent Middlewares — Agent Middleware System

> **Agent Middlewares** is the middleware layer of the EMA AI Agent, situated at key nodes of the Agent execution pipeline. They are responsible for **context enrichment**, **conversation summarization**, and **memory management** — executing before and after model inference via LangChain's AOP-style middleware framework.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Middleware Details](#middleware-details)
  - [ContextEngineHook](#contextenginehook)
  - [Summarization](#summarization)
- [Comparison](#comparison)
- [Workflow (Sequence Diagram)](#workflow-sequence-diagram)
- [Lifecycle](#lifecycle)
- [Core Mechanisms](#core-mechanisms)
- [Data Model](#data-model)
- [Configuration](#configuration)
- [Usage Examples](#usage-examples)
- [FAQ](#faq)
- [Tech Stack](#tech-stack)
- [License](#license)

---

## Overview

### Design Position

Agent Middlewares are built on LangChain's middleware framework (`AgentMiddleware` / `SummarizationMiddleware`). They use **Aspect-Oriented Programming (AOP)** to hook into the Agent execution pipeline, running cross-cutting logic at specific points in each inference cycle.

| Middleware | Timing | Responsibility |
|-----------|--------|---------------|
| `ContextEngineHook` | Before & after Agent inference | Retrieve skill memories from the Context Engine and construct enriched prompts; persist dialogue after inference |
| `Summarization` | Before model call | Summarize context window when conversation history grows too long; trigger user preference extraction |

### Core Capabilities

1. **Context Enrichment** — Before Agent inference, retrieve relevant skills and memories from the Skill Memory Graph and construct an enriched prompt
2. **Conversation Summarization** — Before model call, compress overly long context windows to prevent token overrun
3. **Preference Extraction** — Simultaneously trigger user preference extraction during summarization, writing preferences into the long-term memory store
4. **Auto-Persistence** — Automatically persist each inference turn to MesMemory via `asyncio.create_task`

---

## Architecture

```
Agent Execution Pipeline:

┌─────────────────────────────────────────────────────────────┐
│                    Agent Runtime (LangGraph)                 │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ① abefore_agent()                                           │
│     └─ ContextEngineHook.abefore_agent                       │
│        ├─ Filter out SystemMessages from state["messages"]   │
│        ├─ Extract last HumanMessage content                  │
│        └─ _build_turn_prompt(query_text):                    │
│           ├─ retrieve_history_by_last_n_prompt() → turns     │
│           ├─ build_mixed_query() → enriched query            │
│           └─ assemble() → Skill Memory Graph context         │
│              └─ Prepend result as system prompt to message   │
│                                                              │
│  ② abefore_model()                                           │
│     └─ Summarization.abefore_model (extends SummarizationMW) │
│        ├─ Clone message list, strip SystemMessage            │
│        ├─ Preserve the last HumanMessage                     │
│        ├─ Call parent summarization → reduce_messages       │
│        ├─ Re-insert SystemMessage and last HumanMessage      │
│        ├─ memory_store.load_from_disk()  (before nudge)      │
│        ├─ nudge_messages(session_id, nudge_turn=0)           │
│        └─ memory_store.load_from_disk()  (after nudge)       │
│                                                              │
│  ③ LLM Inference                                             │
│                                                              │
│  ④ aafter_agent()                                            │
│     └─ ContextEngineHook.aafter_agent                        │
│        ├─ slice_last_turn() → extract last dialogue turn     │
│        ├─ sanitize_tool_use_result_pairing() → clean pairs   │
│        ├─ Remove enrichment prefix, restore original input   │
│        ├─ asyncio.create_task(after_turn())   → async learn  │
│        └─ asyncio.create_task(add_messages()) → persist      │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Execution Order

```
1. abefore_agent  (ContextEngineHook)  —→  Context enrichment
2. abefore_model  (Summarization)      —→  Context compression + preference extraction
3. LLM Model Inference
4. aafter_agent   (ContextEngineHook)  —→  Memory persistence + knowledge learning
```

---

## Middleware Details

### ContextEngineHook

**File:** `context_engine_hook.py`

**Class:** `ContextEngineHook(AgentMiddleware)`

Enriches the user message **before** Agent inference and persists the dialogue **after** Agent inference.

#### `__init__(session_id: str)`

```python
hook = ContextEngineHook(session_id="session_001")
```

Stores the session ID and initializes an empty `_turn_prompt` string that will be populated during `abefore_agent`.

---

#### `_build_turn_prompt(query_text: str) -> None`

Internal method that constructs the enrichment prefix by orchestrating three Context Engine calls:

```python
async def _build_turn_prompt(self, query_text: str) -> None:
    # 1. Retrieve recent conversation turns
    recent_messages_addition = retrieve_history_by_last_n_prompt(session_id=self._session_id)

    # 2. Rewrite query with history context (pronouns → entities)
    transformer_query_text = build_mixed_query(
        turns_of_history=recent_messages_addition,
        query=query_text
    )

    # 3. Retrieve Skill Memory Graph context
    assemble_result = await assemble(user_text=transformer_query_text)
    skill_system_prompt_addition = assemble_result.get("system_prompt_addition", "")

    # Build structured content: context + instruction
    self._turn_prompt = textwrap.dedent(f"""\
        {skill_system_prompt_addition}\n\n
        Using the reference materials above (note: they may contain inaccuracies,
        so use them critically), answer the user's actual question below.\n\n
    """)
```

**Key details:**
- The enrichment prefix includes both Skill Memory graph context and a critical-use instruction
- The prefix is stored in `self._turn_prompt` and later removed in `aafter_agent` to prevent context window bloat

---

#### `abefore_agent(state, runtime)`

```
Input: User's original message "How to deploy Docker?"
        │
        ▼
1. Filter out SystemMessages from state["messages"] (reverse iteration, in-place delete)
2. Extract content from the last HumanMessage
3. Handle three message formats:
   ├─ Plain text str       → _build_turn_prompt() + prepend
   ├─ Single media dict    → only enrich "type":"text" portion
   └─ Multimodal list      → find text item, enrich in-place
4. Prepend self._turn_prompt to the original message

Output: "[Skill memory context + instruction] How to deploy Docker?"
```

**Message format support:**

| Input Type | Behavior |
|-----------|----------|
| `str` | Direct enrichment via string concatenation |
| `dict` (single media) | Enrich `text` key in-place |
| `list[dict]` (multimodal) | Find `type="text"` item, enrich in-place |
| Empty/None content | Return `None` (skip) |

---

#### `aafter_agent(state, runtime)`

```
Input: Full inference result message list
        │
        ▼
1. slice_last_turn(all_messages) → extract last dialogue turn
2. sanitize_tool_use_result_pairing(last_turn) → clean tool call/result pairs
3. Extract user_text from the cleaned last human message
4. Remove enrichment prefix: user_text = user_text.removeprefix(self._turn_prompt)
5. Write back the restored original user input to last_human_message.content
6. Extract AI response text from subsequent messages
7. Launch two async tasks concurrently:
   ├─ after_turn(session_id, last_turn_messages)
   │   └─ Skill Memory learning pipeline (knowledge extraction + graph update)
   └─ add_messages(session_id, messages)
       └─ Persist to MesMemory SQLite storage
   └─ await asyncio.gather(task1, task2)
```

**Key details:**

| Concern | Solution |
|---------|----------|
| Non-blocking persistence | `asyncio.create_task` + `asyncio.gather` |
| Context window management | Enrichment prefix removed before storing |
| Tool call integrity | `sanitize_tool_use_result_pairing` fixes unbalanced pairs |
| Multi-format user input | Handles `str`, `dict`, `list[dict]` same as `abefore_agent` |

---

### Summarization

**File:** `summarization.py`

**Class:** `Summarization(SummarizationMiddleware)`

Compresses overly long conversation history before model calls, and triggers user preference extraction during compression.

#### `__init__(session_id: str, **kwargs)`

```python
summarizer = Summarization(session_id="session_001", ...)
```

The `**kwargs` are forwarded to the parent `SummarizationMiddleware` (base compression configuration).

---

#### `abefore_model(state, runtime)`

```
Input: Potentially oversized message list (e.g., 100K+ tokens)
        │
        ▼
1. Copy state + message list (avoid mutating original)
2. Strip SystemMessage → save reference, delete from copy
3. Preserve the last HumanMessage → save reference
4. Call parent SummarizationMiddleware.abefore_model(copy_state, runtime)
   └─ LLM-based summarization of historical messages
   └─ Returns reduce_messages (contains RemoveMessage markers)
5. Re-insert SystemMessage after the first RemoveMessage in reduce_messages
6. If the saved last HumanMessage != the last one in reduce_messages, re-insert it
7. memory_store.load_from_disk()  — sync in-memory state with disk
8. nudge_messages(session_id, nudge_turn=0)  — force preference extraction
9. memory_store.load_from_disk()  — reload to capture nudge writes
10. Return res (the parent's result dict)
```

**Key details:**

| Concern | Solution |
|---------|----------|
| SystemMessage separation | Stripped before compression to avoid polluting semantic density |
| Latest user input preservation | Last `HumanMessage` re-inserted after compression so LLM sees the original question |
| Data consistency | `memory_store.load_from_disk()` called **before and after** nudge to sync in-memory state with disk |
| Force extraction | `nudge_turn=0` bypasses the normal turn-interval check |
| Immutable state | Messages list is cloned to avoid side effects on the original agent state |

**Why system message separation?**

System prompts (character settings, tool definitions, etc.) have a fundamentally different semantic distribution from historical conversation messages. Mixing them into the same compression pass would reduce information density — the summarizer would waste capacity encoding the (unchanging) system prompt alongside the (changing) conversation. Stripping it before compression and re-inserting after yields a significantly better summary quality.

**Why reload memory_store before and after nudge?**

`memory_store` is a singleton in-memory cache backed by markdown files on disk. It can become stale if other agents or processes have written to disk since the last load. Reloading before nudge ensures the extractor sees the latest state; reloading afterward ensures subsequent reads see the newly written preferences.

---

## Comparison

| Feature | ContextEngineHook | Summarization |
|---------|-------------------|---------------|
| **Base Class** | `AgentMiddleware` | `SummarizationMiddleware` |
| **Timing** | Before & after Agent | Before model call |
| **Core Operation** | Context enrichment + persistence | Summarization + preference extraction |
| **Blocking** | Async non-blocking (after part) | Sync blocking |
| **Dependencies** | Context Engine (`assemble`, `after_turn`, `add_messages`) | MesMemory (`nudge_messages`), `memory_store` |
| **Frequency** | Every Agent inference turn | Only when context is too long (parent decides) |
| **Message Mutation** | In-place (enrich + restore) | Clone + modify copy |

---

## Workflow (Sequence Diagram)

```mermaid
sequenceDiagram
    participant User as 用户
    participant Agent as Agent Runtime
    participant CEHook as ContextEngineHook
    participant Summ as Summarization
    participant CE as Context Engine
    participant LLM

    User->>Agent: Send message
    Agent->>CEHook: abefore_agent(state, runtime)
    
    rect rgb(240, 248, 255)
        Note over CEHook: Phase 1: Context Enrichment
        CEHook->>CEHook: Filter SystemMessages
        CEHook->>CE: retrieve_history_by_last_n_prompt()
        CE-->>CEHook: recent turns text
        CEHook->>CE: build_mixed_query(history, query)
        CE-->>CEHook: enriched query
        CEHook->>CE: assemble(enriched_query)
        CE-->>CEHook: system_prompt_addition (XML)
        CEHook->>CEHook: Prepend to user message
    end
    
    Agent->>Summ: abefore_model(state, runtime)
    
    rect rgb(255, 245, 238)
        Note over Summ: Phase 2: Summarization
        Summ->>Summ: Clone messages, strip SystemMessage
        Summ->>Summ: Preserve last HumanMessage
        Summ->>Summ: Call parent summarization
        Summ->>Summ: Re-insert SystemMessage + last HumanMessage
        Summ->>Summ: memory_store.load_from_disk()
        Summ->>CE: nudge_messages(session_id, nudge_turn=0)
        Summ->>Summ: memory_store.load_from_disk()
    end
    
    Agent->>LLM: Invoke model
    
    rect rgb(240, 248, 255)
        Note over Agent: Phase 3: LLM inference
        LLM-->>Agent: Response
    end
    
    Agent->>CEHook: aafter_agent(state, runtime)
    
    rect rgb(240, 248, 255)
        Note over CEHook: Phase 4: Post-processing
        CEHook->>CEHook: slice_last_turn()
        CEHook->>CEHook: sanitize_tool_use_result_pairing()
        CEHook->>CEHook: Remove enrichment prefix, restore input
        par Async persistence
            CEHook->>CE: after_turn(session_id, messages)
            CEHook->>CE: add_messages(session_id, messages)
        end
    end
    
    Agent->>User: Reply
```

---

## Lifecycle

| Phase | ContextEngineHook | Summarization |
|-------|-------------------|---------------|
| **Before Agent** | Strip system messages → extract query → build enrichment via Context Engine → prepend to user message | — |
| **Before Model** | — | Clone state → strip system → preserve last human → call parent compression → re-insert system + human → reload memory_store → nudge → reload memory_store |
| **LLM Inference** | — | — |
| **After Agent** | Extract last turn → clean tool pairs → restore original input → `after_turn()` (async skill learn) → `add_messages()` (async persist) | — |

---

## Core Mechanisms

### 1. AOP-based Middleware Hook

Both middlewares use LangChain's AOP-style middleware framework. `ContextEngineHook` extends `AgentMiddleware` to hook into the Agent lifecycle (`abefore_agent` / `aafter_agent`). `Summarization` extends `SummarizationMiddleware` to hook into the model lifecycle (`abefore_model`).

This design allows cross-cutting concerns (memory, compression) to be cleanly separated from the core Agent logic without modifying the Agent itself.

### 2. Three-format Message Support

The `ContextEngineHook` handles three distinct message content formats transparently:

| Format | Example | Enrichment Strategy |
|--------|---------|-------------------|
| `str` | `"How to deploy?"` | String concatenation |
| `dict` | `{"type": "text", "text": "Hello"}` | In-place `text` key modification |
| `list[dict]` | `[{"type": "text", ...}, {"type": "image_url", ...}]` | Find text item, enrich in-place |

This ensures compatibility with both text-only and multimodal workflows.

### 3. Enrichment Prefix Lifecycle

The enrichment prefix is injected in `abefore_agent` and stripped in `aafter_agent`:

```
Injection (abefore_agent):
  "[Skill context + instruction] How to deploy Docker?"
                                                       ↑ enrichment
Removal (aafter_agent):
  user_text.removeprefix(self._turn_prompt)
  → "How to deploy Docker?"   ← original restored
```

This prevents the enrichment prefix from accumulating in MesMemory across turns, which would otherwise rapidly consume the context window.

### 4. Force Nudge on Compression

Summarization forces preference extraction during compression via `nudge_turn=0`. This is a deliberate trade-off:

- **Without force**: Preferences embedded in old conversation turns would be lost when those turns are compressed into a summary
- **With force**: Latent preferences (e.g., "I prefer terse answers") are extracted and persisted before the original messages are replaced by a summary

### 5. Async Non-blocking Post-processing

`ContextEngineHook.aafter_agent` launches `after_turn()` and `add_messages()` as concurrent `asyncio.create_task` calls, gathered via `asyncio.gather`. This ensures:

- The Agent's response latency is not affected by persistence or knowledge extraction
- Both tasks run concurrently (extraction and persistence in parallel)
- If either task fails, the exception propagates through `asyncio.gather` (no silent swallowing)

---

## Data Model

### State Message Types

```python
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage, RemoveMessage
```

| Type | Role in Middleware |
|------|-------------------|
| `SystemMessage` | Stripped before enrichment (ContextEngineHook) and before compression (Summarization) to prevent pollution |
| `HumanMessage` | Source of user query for enrichment; last one preserved during compression |
| `AIMessage` | Source of AI response extracted in `aafter_agent` |
| `RemoveMessage` | Marker inserted by parent `SummarizationMiddleware` to mark messages for removal |

### ContextEnrichment State

```
self._turn_prompt: str
  └─ Enrichment prefix built during abefore_agent
  └─ Format: [skill_memory_context] + instruction_text
  └─ Used in: abefore_agent (prepend) → aafter_agent (removeprefix)
```

### Memory Store State

`memory_store` is a singleton module-level object (`from tools import memory_store`) managed by the `Summarization` middleware:

- **Type**: In-memory cache backed by markdown files on disk
- **Read**: `memory_store.load_from_disk()` — synchronizes in-memory state with disk
- **Write**: `nudge_messages()` — writes extracted preferences to markdown files
- **Consistency**: Loaded before and after nudge to prevent stale reads

---

## Configuration

| Config | ContextEngineHook | Summarization |
|--------|-------------------|---------------|
| **Session ID** | `session_id` (constructor) | `session_id` (constructor) |
| **History turns** | N/A (delegates to `retrieve_history_by_last_n_prompt()` — default 5) | — |
| **Nudge force** | — | `nudge_turn=0` (always forces extraction) |
| **Message format support** | `str`, `dict`, `list[dict]` | `list[BaseMessage]` (standard LangGraph format) |
| **Parent config** | — | Forwarded via `**kwargs` to `SummarizationMiddleware` |

---

## Usage Examples

### Registering Middlewares

```python
from agent.middlewares import ContextEngineHook, Summarization

# Create middleware instances
context_hook = ContextEngineHook(session_id="session_001")
summarizer = Summarization(session_id="session_001")

# Register with LangGraph Runtime
# The Runtime accepts middleware during construction or via add_middleware
runtime = Runtime(
    agent=my_agent,
    middlewares=[context_hook, summarizer]
    # Execution order: ContextEngineHook.abefore_agent →
    # Summarization.abefore_model → LLM → ContextEngineHook.aafter_agent
)
```

### Standalone ContextEngineHook Usage

```python
from agent.middlewares import ContextEngineHook

hook = ContextEngineHook(session_id="session_001")

# Usually called by LangGraph Runtime, but can be invoked directly for testing:
await hook.abefore_agent(state, runtime)
# → state["messages"][-1].content is now enriched

# ... after LLM inference ...
await hook.aafter_agent(state, runtime)
# → dialogue persisted to MesMemory, Skill Memory updated
```

### Standalone Summarization Usage

```python
from agent.middlewares import Summarization
from langgraph.runtime import Runtime

summarizer = Summarization(
    session_id="session_001",
    # Additional SummarizationMiddleware kwargs go here
)

# Called by LangGraph Runtime before model inference:
await summarizer.abefore_model(state, runtime)
# → Long context compressed, preferences extracted
```

---

## FAQ

### Q1: Why does ContextEngineHook filter out SystemMessages?

In `abefore_agent`, SystemMessages are filtered out to prevent system prompts (character settings, tool definitions, etc.) from being passed as query context to the Context Engine. This ensures Skill Memory and long-term memory retrieval accuracy. The `system_prompt_addition` is returned separately via the enrichment prefix.

### Q2: Why does Summarization force preference extraction during compression?

Compression means the context window is shrinking, and old conversation history will be replaced by summaries. If preferences are not extracted at this moment, details (like explicitly stated user preferences) are permanently lost. Forcing extraction ensures preferences are persisted to the long-term memory store even after the original conversation is summarized.

### Q3: What are the risks of using `asyncio.create_task` in `aafter_agent`?

`after_turn` and `add_messages` run asynchronously via `asyncio.create_task` and are gathered with `asyncio.gather`. Unlike raw `create_task` (which can silently swallow exceptions), `gather` propagates exceptions. However:
- If the Agent process exits abnormally between `create_task` and `gather`, incomplete tasks may still be lost
- The `gather` ensures both tasks complete before `aafter_agent` finishes — so exception handling is covered
- This is an accepted trade-off: post-processing reliability is bounded by the async event loop lifecycle

### Q4: How is middleware execution order guaranteed?

The execution order is governed by the middleware chain inside LangGraph's Runtime. The ordering is:
1. `abefore_agent` → `abefore_model` → LLM → `aafter_agent`
2. Multiple middlewares in the same phase execute in registration order

### Q5: What happens if `_build_turn_prompt` fails?

If `_build_turn_prompt` throws an exception (e.g., Context Engine unavailable), `abefore_agent` will propagate the error upward to the LangGraph Runtime. The middleware framework does not catch exceptions by default — if enrichment is critical, the caller should handle the error at the runtime level.

### Q6: Why clone the message list in Summarization?

The Summarization middleware clones the messages list before modifying it to avoid side effects on the original `state["messages"]`. This is important because:
- The parent `SummarizationMiddleware.abefore_model` expects a mutable copy it can freely modify
- The original state should remain untouched until the runtime officially applies middleware results
- Cloning prevents bugs where downstream handlers see partially-modified state

### Q7: What happens to multimodal content during enrichment?

For `list[dict]` (multimodal) messages, only the `type="text"` portion is enriched. Images and other media items pass through unchanged. The enrichment is written back in-place into the same text item, preserving the original message structure.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Middleware Framework** | LangChain `AgentMiddleware` / `SummarizationMiddleware` |
| **Agent Runtime** | LangGraph `Runtime` |
| **Message Model** | LangChain `BaseMessage` / `SystemMessage` / `HumanMessage` / `AIMessage` / `RemoveMessage` |
| **Memory System** | Context Engine (Skill Memory Graph + MesMemory) |
| **Storage (MesMemory)** | SQLite + FTS5 |
| **Storage (Memory Store)** | Markdown files (`.md`) on disk, loaded into in-memory singleton |
| **Async Framework** | `asyncio.create_task` + `asyncio.gather` |
| **Utility** | `textwrap.dedent` (enrichment prompt formatting) |
| **External Helpers** | `pub_func.slice_last_turn`, `pub_func.sanitize_tool_use_result_pairing` |

---

## License

This project is licensed under the MIT License (following the EMA AI Agent license).

---

**Author:** MOYE  
**Last updated:** 2026-06-02
