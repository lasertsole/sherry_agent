# Agent Middlewares — Agent Middleware System

> **Agent Middlewares** is the middleware layer of the EMA AI Agent, situated at key nodes of the Agent execution pipeline. They are responsible for **context enrichment**, **conversation summarization**, and **memory management**—executing before and after model inference.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Middleware Details](#middleware-details)
- [Tech Stack](#tech-stack)
- [FAQ](#faq)

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
3. **Memory Extraction** — Simultaneously trigger user preference extraction during summarization, writing preferences into the long-term memory store
4. **Auto-Persistence** — Automatically persist each inference turn to MesMemory

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
│        ├─ retrieve_history_by_last_n_prompt() → recent turns │
│        ├─ build_mixed_query() → build enriched query         │
│        └─ assemble() → retrieve from Skill Memory Graph     │
│           └─ Prepend result as system prompt to user message │
│                                                              │
│  ② abefore_model()                                           │
│     └─ Summarization.abefore_model (extends SummarizationMW) │
│        ├─ Clone message list, strip system message           │
│        ├─ Preserve the last HumanMessage                     │
│        ├─ Call parent summarization → reduce_messages       │
│        ├─ Re-insert system message and last human message    │
│        ├─ Reload memory_store from disk                      │
│        ├─ Call nudge_messages() → extract user preferences   │
│        └─ Reload memory_store again                          │
│                                                              │
│  ③ LLM Inference                                             │
│                                                              │
│  ④ aafter_agent()                                            │
│     └─ ContextEngineHook.aafter_agent                        │
│        ├─ slice_last_turn() → extract last dialogue turn     │
│        ├─ sanitize_tool_use_result_pairing() → clean tool    │
│        ├─ Restore original user input (remove enrichment)    │
│        ├─ after_turn() → async post-processing (skill learn) │
│        └─ add_messages() → persist to MesMemory              │
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

#### `abefore_agent(state, runtime)`

```
Input: User's original message "How to deploy Docker?"
        │
        ▼
1. Filter out SystemMessages from state["messages"]
2. Extract content from the last HumanMessage
3. _build_turn_prompt(query_text):
   ├─ retrieve_history_by_last_n_prompt() → last 5 turns
   ├─ build_mixed_query(history + query)  → build enriched query
   └─ assemble(enriched query)            → retrieve from Skill Memory Graph
      └─ Returns system_prompt_addition (relevant skills + long-term memory)
4. Prepend system_prompt_addition to the original user message

Output: "[Skill memory + Long-term memory] How to deploy Docker?"
```

**Key Details:**

- Supports multiple message formats: plain text `str`, single media `dict`, multimodal list `list[dict]`
- For multimodal messages (e.g., image + text), only the `type="text"` portion is enriched; the result is written back in place
- System messages are removed before enrichment to avoid polluting the retrieval process

#### `aafter_agent(state, runtime)`

```
Input: Full inference result message list
        │
        ▼
1. slice_last_turn() → extract last dialogue turn (user + AI + tool calls)
2. sanitize_tool_use_result_pairing() → ensure tool call/result pairs are correct
3. Remove the enriched prefix from the user message, restoring the original input
4. Execute asynchronously:
   ├─ after_turn(session_id, last_turn_messages)
   │   └─ Trigger Skill Memory learning pipeline (knowledge extraction + graph update)
   └─ add_messages(session_id, messages)
       └─ Persist to MesMemory SQLite storage
```

**Key Details:**

- Uses `asyncio.create_task` for non-blocking async post-processing
- The user input is restored to its original form before persistence (enrichment prefix removed) to prevent prompt tokens from filling the context window
- `sanitize_tool_use_result_pairing` cleans tool call pairings for data integrity

---

### Summarization

**File:** `summarization.py`

**Class:** `Summarization(SummarizationMiddleware)`

Compresses overly long conversation history before model calls, and triggers user preference extraction during compression.

#### `abefore_model(state, runtime)`

```
Input: Potentially oversized message list (e.g., 100K+ tokens)
        │
        ▼
1. Clone the message list (avoid mutating the original state)
2. Strip SystemMessage (save and re-insert later to avoid polluting compression)
3. Preserve the last HumanMessage (ensure the latest question is not lost)
4. Call parent SummarizationMiddleware.abefore_model
   └─ LLM-based summarization of historical messages
   └─ Returns reduce_messages (includes RemoveMessage markers)
5. Re-insert SystemMessage (after the first RemoveMessage)
6. If the last HumanMessage was not preserved, re-insert it
7. Reload memory_store from disk
8. Call nudge_messages(session_id, nudge_turn=0) → force preference extraction
9. Reload memory_store again
```

**Key Details:**

- **Why strip SystemMessage?** System prompts (character settings, tool definitions, etc.) have a different semantic distribution from historical messages. Mixing them reduces the information density of the compressed summary.
- **Why reload memory_store before and after?** Ensures the in-memory state matches disk files before and after preference extraction, avoiding data inconsistency from concurrent reads/writes.
- **`nudge_turn=0`**: Force-triggers preference extraction regardless of turn interval.
- **Preserve the latest HumanMessage**: Prevents the most recent user input from being summarized away, ensuring the LLM sees the original question.

---

## Middleware Comparison

| Feature | ContextEngineHook | Summarization |
|---------|-------------------|---------------|
| **Base Class** | `AgentMiddleware` | `SummarizationMiddleware` |
| **Timing** | Before & after Agent | Before model call |
| **Core Operation** | Context enrichment + persistence | Summarization + preference extraction |
| **Blocking** | Async non-blocking (after part) | Sync blocking |
| **Dependencies** | Context Engine (assemble, after_turn) | MesMemory (nudge_messages) |
| **Frequency** | Every Agent inference turn | Only when context is too long (parent decides) |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Middleware Framework** | LangChain `AgentMiddleware` / `SummarizationMiddleware` |
| **Agent Runtime** | LangGraph `Runtime` |
| **Message Model** | LangChain `BaseMessage` / `SystemMessage` / `HumanMessage` / `AIMessage` |
| **Memory System** | Context Engine (Skill Memory Graph + MesMemory) |
| **Storage** | MesMemory SQLite + Memory Store (markdown files) |

---

## FAQ

### Q1: Why does ContextEngineHook filter out SystemMessages?

In `abefore_agent`, SystemMessages are filtered out to prevent system prompts (character settings, tool definitions, etc.) from being passed as query context to the Context Engine. This ensures Skill Memory and long-term memory retrieval accuracy. The `system_prompt_addition` is returned separately via the middleware return value.

---

### Q2: Why does Summarization force preference extraction during compression?

Compression means the context window is shrinking, and old conversation history will be replaced by summaries. If preferences are not extracted at this moment, details (like explicitly stated user preferences) are permanently lost. Forcing extraction ensures preferences are persisted to the long-term memory store even after the original conversation is summarized.

---

### Q3: What are the risks of using `asyncio.create_task` in `aafter_agent`?

`after_turn` and `add_messages` run asynchronously via `asyncio.create_task` to avoid blocking the Agent's response. This means:
- If the Agent process exits abnormally, incomplete async tasks may be lost
- If an async task raises an exception, it may be silently swallowed by `asyncio`
- This is an accepted trade-off: response speed is prioritized over post-processing reliability

---

### Q4: How is middleware execution order guaranteed?

The execution order is governed by the middleware chain inside LangGraph's Runtime. The ordering is:
1. `abefore_agent` → `abefore_model` → LLM → `aafter_agent`
2. Multiple middlewares in the same phase execute in registration order

---

## License

This project is licensed under the MIT License (following the EMA AI Agent license).

---

**Last updated:** 2026-05-30
