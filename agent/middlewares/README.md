# EMA Agent Middleware System

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangGraph 1.2+](https://img.shields.io/badge/LangGraph-1.2%2B-orange)]()

A composable middleware pipeline for LLM agent execution — message management, tool call validation, guardrails, timeouts, budget control, and multimodal processing. All middleware hooks into the **LangGraph agent lifecycle** via a shared, persistent state system.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Middleware Chain](#middleware-chain)
- [Middleware Reference](#middleware-reference)
  - [SummarizationMiddleware](#summarizationmiddleware)
  - [ToolCallNormalizeMiddleware](#toolcallnormalizemiddleware)
  - [ToolGuardrailsMiddleware](#toolguardrailsmiddleware)
  - [ToolTimeoutMiddleware](#tooltimeoutmiddleware)
  - [IterationBudgetMiddleware](#iterationbudgetmiddleware)
  - [MultimodalProcessorMiddleware](#multimodalprocessormiddleware)
  - [ContextEngineHook](#contextenginehook)
- [Shared State System](#shared-state-system)
- [Configuration](#configuration)
- [Lifecycle & Data Flow](#lifecycle--data-flow)
- [Writing a Custom Middleware](#writing-a-custom-middleware)

---

## Architecture Overview

All middleware inherits from a dedicated **base class** (e.g., `SummarizationMiddleware`, `AgentMiddleware`, or `ContextEngineHook`), and each implements one or more of the following **lifecycle hooks**:

| Hook | Called When | Purpose |
|---|---|---|
| `awrap_before_agent(state)` | Before every LLM call | Prepare state, inject system prompt, prune history |
| `awrap_after_agent(state)` | After every LLM call | Post-process assistant response, run side effects |
| `awrap_tool_call(state, tool_call)` | Before each individual tool execution | Validate, guard, timeout, or enrich tool calls |
| `awrap_after_tool(state)` | After a tool returns | Process tool result, check budgets, add computed fields |

Middleware instances are registered in `langgraph.json` under the `middlewares` key and **execute in declaration order** as a chain wrapping the agent node.

### State Persistence

Middleware communicates via two cross-hook state dictionaries:

- **`state_register_mem`** — per-conversation memory (persisted across turns). Used for counters, budgets, guardrail tracking.
- **`state_register_db`** — per-conversation database records (structured records). Used for message storage references.

These dictionaries are carried in the **agent state** (`AgentState`) and passed through every hook method.

---

## Middleware Chain

The full pipeline executes in this order (each wraps the inner layers):

```
┌─────────────────────────────────────────────────────────┐
│  SummarizationMiddleware      (outermost — prune first) │
│  ToolCallNormalizeMiddleware  (repair broken tool calls)│
│  ToolGuardrailsMiddleware     (detect loops, halt)      │
│  ToolTimeoutMiddleware        (per-call timeout)        │
│  IterationBudgetMiddleware    (hard iteration cap)      │
│  MultimodalProcessorMiddleware(media handling)          │
│  ContextEngineHook            (memory & nudge, innermost)│
│    ┌─────────────────────────────────────┐              │
│    │         LLM (Agent Node)            │              │
│    └─────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────┘
```

**Data flow (single turn):**

1. `awrap_before_agent` — runs from outer to inner (summarization first, context engine last)
2. LLM generates a response (may include tool calls)
3. `awrap_after_agent` — runs from inner to outer (context engine first, summarization last)
4. For each tool call: each middleware's `awrap_tool_call` fires in order
5. After each tool returns: each middleware's `awrap_after_tool` fires in order
6. Repeat from step 1 until LLM produces a final answer or budget exhausted

---

## Middleware Reference

### SummarizationMiddleware

**File:** `summarization.py`  
**Base:** `SummarizationMiddleware`  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`

Prunes conversation history when it exceeds token limits, preserving recent turns and generating a compressed summary of older messages.

**Behavior:**

- On `awrap_before_agent`: counts total tokens in the message history. If above the configured `max_tokens`, it:
  1. Keeps the last N turns of recent conversation intact
  2. Compresses everything before that into a summarization prompt
  3. Injects the summary as a `SystemMessage` at the start of the message list
- On `awrap_after_agent`: stores the summarization result into `state_register_mem["summarization"]["current_summary"]`

**Configuration:**

```json
{
  "summarization": {
    "max_tokens": 64000,
    "recent_turns": 10
  }
}
```

---

### ToolCallNormalizeMiddleware

**File:** `tool_call_normalize.py`  
**Base:** `AgentMiddleware`  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

Repairs malformed tool calls — primarily fixes **unpaired ID/name patterns** where the LLM produces tool calls with incorrect or mismatched `id`/`name` fields.

**Behavior:**

- **Pair Repair:** When a `tool_call` has multiple entries where `tool_call` `id` values don't match expected pattern, the middleware builds a mapping from name → expected id and reassigns them.
- **Deduplication:** Skips tool calls already processed (tracked in `state_register_mem["tool_call_normalize"]["last_names"]`).
- **Noise Reduction:** Strips tool call entries that fail validation.

**Why Needed:** LLMs (especially smaller or quantized models) frequently produce tool calls with dangling, swapped, or duplicated `id` fields. This middleware resolves those issues silently before they reach the runtime.

---

### ToolGuardrailsMiddleware

**File:** `tool_guardrails.py`  
**Base:** `AgentMiddleware`  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

Detects and prevents **infinite tool-call loops**, **repeated failure patterns**, and **identical retries**. Uses a three-tier escalation system.

**Tiers:**

| Tier | Condition | Action |
|---|---|---|
| `warn` | Tool name repeated 3+ times across recent calls (same tool, same name, any args) | Injects a warning `SystemMessage` into the conversation before the next LLM call |
| `block` | Same tool + same arguments repeated 3+ times | Prevents the tool call from executing — returns an error `ToolMessage` instead |
| `halt` | Blocked calls keep being regenerated (3+ blocks in a row) | Forces a **hard stop**: raises `AgentHalt` which terminates the agent run |

**Detection Data:**

- Tracks tool call names and serialized arguments in `state_register_mem["tool_guardrails"]`
- Uses a sliding-window approach — only the most recent calls are considered (window size configurable)

**Configuration:**

```json
{
  "tool_guardrails": {
    "call_window": 15,
    "warn_threshold": 4,
    "block_threshold": 3,
    "halt_threshold": 3
  }
}
```

---

### ToolTimeoutMiddleware

**File:** `tool_timeout.py`  
**Base:** `AgentMiddleware`  
**Hooks:** `awrap_tool_call`, `awrap_after_tool`

Enforces a **maximum execution time per tool call**. If a tool exceeds its timeout, the middleware cancels execution and returns a timeout error `ToolMessage`.

**Behavior:**

- On `awrap_tool_call`: records the start time in `state_register_mem["tool_timeout"]["tool_start_time"]`
- On `awrap_after_tool`: checks elapsed time against the configured `timeout_seconds`. If exceeded, replaces the result with a timeout error message.

**Configuration:**

```json
{
  "tool_timeout": {
    "timeout_seconds": 60
  }
}
```

---

### IterationBudgetMiddleware

**File:** `iteration_budget.py`  
**Base:** `AgentMiddleware`  
**Hooks:** `awrap_before_agent`, `awrap_after_tool`

Enforces a **hard cap on the number of LLM-to-tool iterations** per conversation turn. Once the limit is reached, the agent is forced to produce a final answer without further tool calls.

**Behavior:**

- On `awrap_before_agent`: checks the current iteration count against the `max_iterations` limit. If exceeded, appends a `SystemMessage` instructing the LLM to answer immediately using available information.
- On `awrap_after_tool`: increments the iteration counter in `state_register_mem["iteration_budget"]["count"]`.
- **Iteration counter resets** on the next `awrap_before_agent` call after the limit was hit (detected via a "resetting" flag).

**Configuration:**

```json
{
  "iteration_budget": {
    "max_iterations": 10
  }
}
```

---

### MultimodalProcessorMiddleware

**File:** `multimodal_processor.py`  
**Base:** `AgentMiddleware`  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_after_tool`

Handles **multimodal content** (images, files) in the conversation — normalizes media URIs from various sources (local files, S3, HTTP) into a format consumable by the LLM.

**Behavior:**

- Detects media references in user messages (file paths, S3 URIs, HTTP URLs)
- Resolves and encodes media into base64 data URIs for LLM consumption
- Strips resolved URIs from the conversation after the LLM call to keep history clean
- Supports: local file system, S3-compatible storage, HTTP/HTTPS URLs

**Configuration:**

```json
{
  "multimodal_processor": {
    "enabled": true,
    "max_image_size_mb": 20,
    "allowed_mime_types": ["image/jpeg", "image/png", "image/webp", "image/gif"]
  }
}
```

---

### ContextEngineHook

**File:** `context_engine/core.py`, `context_engine/nudge.py`  
**Base:** `ContextEngineHook`  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`

The **innermost middleware** — closest to the LLM. Manages system prompts, conversation persistence, and periodic **nudge** interventions (memory review, skill review).

#### Core Behavior (`core.py`)

- **`awrap_before_agent`:**
  1. Loads the **system prompt** from cache (`MemoryCache`), with fallback to a default
  2. **Sets thread-safe variables** (`conversation_id`, `user_id`) for downstream hooks
  3. Loads any saved messages from `MesMemory` and merges them into the conversation state
  4. Injects the (optionally customized) system prompt as the first `SystemMessage`
- **`awrap_after_agent`:**
  1. Persists the assistant response to `MesMemory` via `add_messages()`
  2. Runs the **nudge system** (see below) as a post-agent side effect
- **`awrap_tool_call` (nudge side effect):** increments a skill-review counter in `MesMemory` each time a tool is called

#### Nudge System (`nudge.py`)

Periodically injects intervention messages to encourage the user to engage with the memory/skill system.

| Nudge Type | Trigger | Content |
|---|---|---|
| **Memory Nudge** | Every 10 turns since last memory operation | Prompts the user to update their memory/system prompt for better results |
| **Skill Review Nudge** | Every 10 tool calls since last review | Prompts the user to rate the last tool execution result |
| **Combined Nudge** | Both conditions met simultaneously | Merged message covering both memory and skill review |

**Lock Mechanism:** Each nudge type has a **cooldown lock** in `MesMemory` (`nudge_lock_memory`, `nudge_lock_skill`) that prevents repeated nudges within the 10-turn window. The lock is reset when the user actually performs the action (e.g., updates memory or rates a skill).

Nudge messages are sent as **separate `AIMessage` chunks** appended after the normal assistant response, so they appear as natural follow-up suggestions in the UI.

**Configuration (in `system/config.tool.md`):**

```json
{
  "context_engine": {
    "enabled": true,
    "nudge_memory_interval": 10,
    "nudge_skill_interval": 10
  }
}
```

#### Dependencies

- **`MemoryCache`** — thread-safe in-memory cache for system prompts and metadata
- **`MesMemory`** — persistent conversation memory backend (stores messages, nudge locks, skill review counters)
- **`system/config.tool.md`** — configuration file for system prompt and nudge intervals

---

## Shared State System

All middleware access two shared dictionaries on the agent state:

| Key | Type | Persistence | Purpose |
|---|---|---|---|
| `state_register_mem` | `dict` | Per-conversation (in-memory) | Counters, flags, current summaries, window buffers |
| `state_register_db` | `dict` | Per-conversation (DB-backed) | Message IDs, structured records |

**Namespace convention** — each middleware uses its own top-level key:

```
state_register_mem = {
    "summarization": { "current_summary": "..." },
    "tool_call_normalize": { "last_names": [...] },
    "tool_guardrails": { "tool_calls": [...], "block_count": 3 },
    "tool_timeout": { "tool_start_time": 1234567890.0 },
    "iteration_budget": { "count": 5 },
    "multimodal_processor": { "resolved_uris": [...] },
}
```

---

## Configuration

Middleware configuration is loaded from two sources:

1. **`langgraph.json`** — middleware registration and global settings
2. **`system/config.tool.md`** — per-middleware parameters (parsed at startup)

### langgraph.json Example

```json
{
  "middlewares": [
    "agent.middlewares.summarization.SummarizationMiddleware",
    "agent.middlewares.tool_call_normalize.ToolCallNormalizeMiddleware",
    "agent.middlewares.tool_guardrails.ToolGuardrailsMiddleware",
    "agent.middlewares.tool_timeout.ToolTimeoutMiddleware",
    "agent.middlewares.iteration_budget.IterationBudgetMiddleware",
    "agent.middlewares.multimodal_processor.MultimodalProcessorMiddleware",
    "agent.middlewares.context_engine.core.ContextEngineHook"
  ]
}
```

### config.tool.md Format

```yaml
middleware:
  summarization:
    max_tokens: 64000
    recent_turns: 10
  tool_guardrails:
    call_window: 15
    warn_threshold: 4
    block_threshold: 3
    halt_threshold: 3
  tool_timeout:
    timeout_seconds: 60
  iteration_budget:
    max_iterations: 10
  multimodal_processor:
    enabled: true
    max_image_size_mb: 20
  context_engine:
    enabled: true
    nudge_memory_interval: 10
    nudge_skill_interval: 10
```

---

## Lifecycle & Data Flow

### Single Turn (Detailed)

```
[User sends message]
    │
    ▼
Summarization.awrap_before_agent(state)
    │  prune history if over token budget
    ▼
ToolCallNormalize.awrap_before_agent(state)
    │  (no-op typically)
    ▼
ToolGuardrails.awrap_before_agent(state)
    │  inject warning if loop detected
    ▼
ToolTimeout.awrap_before_agent(state)
    │  (no-op typically)
    ▼
IterationBudget.awrap_before_agent(state)
    │  inject "answer immediately" if over budget
    ▼
MultimodalProcessor.awrap_before_agent(state)
    │  resolve media URIs → base64
    ▼
ContextEngineHook.awrap_before_agent(state)
    │  load system prompt, restore conversation, set thread vars
    ▼
┌──────────────────────────────────────────────┐
│              LLM CALL (Agent Node)            │
│  Returns: assistant message (text + tool_calls)│
└──────────────────────────────────────────────┘
    │
    ▼
ContextEngineHook.awrap_after_agent(state)
    │  persist to MesMemory, run nudge
    ▼
MultimodalProcessor.awrap_after_agent(state)
    │  clean up resolved URIs from history
    ▼
IterationBudget.awrap_after_agent(state)
    │  (no-op typically)
    ▼
ToolTimeout.awrap_after_agent(state)
    │  (no-op typically)
    ▼
ToolGuardrails.awrap_after_agent(state)
    │  update tool call history window
    ▼
ToolCallNormalize.awrap_after_agent(state)
    │  update last_names tracking
    ▼
Summarization.awrap_after_agent(state)
    │  store summary result
    │
    ▼
[For each tool_call in assistant message:]
    │
    ├─ ToolCallNormalize.awrap_tool_call(state, tc)
    │     repair malformed id/name pairs
    ├─ ToolGuardrails.awrap_tool_call(state, tc)
    │     block or halt if threshold exceeded
    ├─ ToolTimeout.awrap_tool_call(state, tc)
    │     record start time
    ├─ IterationBudget.awrap_tool_call(state, tc)
    │     (no-op typically)
    ├─ MultimodalProcessor.awrap_tool_call(state, tc)
    │     (no-op typically)
    └─ ContextEngineHook.awrap_tool_call(state, tc)
           increment skill-review counter (nudge side effect)
    │
    ▼
    [Tool executes]
    │
    ▼
    [For each tool result:]
    │
    ├─ ToolTimeout.awrap_after_tool(state)
    │     check elapsed vs timeout
    ├─ IterationBudget.awrap_after_tool(state)
    │     increment iteration counter
    ├─ ToolGuardrails.awrap_after_tool(state)
    │     register result for future detection
    ├─ ToolCallNormalize.awrap_after_tool(state)
    │     (no-op typically)
    ├─ Summarization.awrap_after_tool(state)
    │     (no-op typically)
    ├─ MultimodalProcessor.awrap_after_tool(state)
    │     (no-op typically)
    └─ ContextEngineHook.awrap_after_tool(state)
           (runs via awrap_after_agent, not per-tool)
    │
    ▼
[Loop back to before_agent for next iteration until final answer]
```

---

## Writing a Custom Middleware

```python
from agent.middlewares.base import AgentMiddleware

class MyCustomMiddleware(AgentMiddleware):
    """Custom middleware example."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.my_param = config.get("my_param", "default")

    async def awrap_before_agent(self, state: AgentState) -> AgentState:
        # Runs before each LLM call
        state.state_register_mem["my_middleware"] = {"started": True}
        return state

    async def awrap_after_agent(self, state: AgentState) -> AgentState:
        # Runs after each LLM call
        return state

    async def awrap_tool_call(
        self, state: AgentState, tool_call: ToolCall
    ) -> AgentState:
        # Runs before each individual tool execution
        if tool_call["name"] == "sensitive_tool":
            # Add guard logic here
            pass
        return state

    async def awrap_after_tool(
        self, state: AgentState
    ) -> AgentState:
        # Runs after each tool returns
        return state
```

Register it in `langgraph.json`:

```json
{
  "middlewares": [
    "agent.middlewares.my_custom.MyCustomMiddleware"
  ]
}
```

---

## Appendix

### File Layout

```
agent/middlewares/
├── __init__.py
├── summarization.py              # SummarizationMiddleware
├── tool_call_normalize.py        # ToolCallNormalizeMiddleware
├── tool_guardrails.py            # ToolGuardrailsMiddleware
├── tool_timeout.py               # ToolTimeoutMiddleware
├── iteration_budget.py           # IterationBudgetMiddleware
├── multimodal_processor.py       # MultimodalProcessorMiddleware
├── context_engine/
│   ├── __init__.py
│   ├── core.py                   # ContextEngineHook (main)
│   ├── nudge.py                  # Nudge logic (memory/skill review)
│   └── ...
├── base/                         # Base classes
│   └── ...
├── README.md                     # This file
└── README.zh.md                  # Chinese version
```
