# EMA Agent Middleware System

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangGraph 1.2+](https://img.shields.io/badge/LangGraph-1.2%2B-orange)]()

A composable middleware pipeline for LLM agent execution — message management, tool call validation, guardrails, budget control, multimodal processing, and heartbeat monitoring. All middleware hooks into the **LangGraph agent lifecycle** via a shared, persistent state system.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Middleware Chain](#middleware-chain)
- [Middleware Reference](#middleware-reference)
  - [Summarization](#summarization)
  - [ToolCallNormalize](#toolcallnormalize)
  - [ToolGuardrails](#toolguardrails)
  - [IterationBudget](#iterationbudget)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [MultimodalProcessor](#multimodalprocessor)
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
| `awrap_tool_call(state, tool_call)` | Before each individual tool execution | Validate, guard, or enrich tool calls |
| `awrap_after_tool(state)` | After a tool returns | Process tool result, check budgets, add computed fields |

Middleware instances are registered in the **main agent builder** and **execute in declaration order** as a chain wrapping the agent node.

### State Persistence

Middleware communicates via two cross-hook state dictionaries provided as **singleton instances** from `runtime`:

- **`state_register_mem`** (`StateRegisterMeM`) — in-memory, per-session state store. Used for counters, budgets, guardrail tracking, heartbeat progress.
- **`state_register_db`** (`StateRegisterDB`) — SQLite-backed, per-session state store. Used for structured records that need persistence across process restarts.

Both are singletons imported from `runtime.state_register`. They share the same `Register` interface (`set_state`, `get_state`, `delete_state`, `clear_session`, etc.).

---

## Middleware Chain

The full pipeline for the **main agent** executes in this order (each wraps the inner layers):

```
┌─────────────────────────────────────────────────────────┐
│  Summarization                (outermost — prune first) │
│  ToolCallNormalize            (repair broken tool calls)│
│  ToolGuardrails               (detect loops, halt)      │
│  IterationBudget              (hard iteration cap)      │
│  HeartbeatStaleness           (heartbeat timeout)       │
│  MultimodalProcessor          (media handling)          │
│  ContextEngineHook            (memory & nudge, innermost)│
│    ┌─────────────────────────────────────┐              │
│    │         LLM (Agent Node)            │              │
│    └─────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────┘
```

> **Note:** `HeartbeatStaleness` is used by **worker agents** only (not the main agent). It monitors subagent progress and terminates agents that become idle/unresponsive.

**Data flow (single turn):**

1. `awrap_before_agent` — runs from outer to inner (summarization first, context engine last)
2. LLM generates a response (may include tool calls)
3. `awrap_after_agent` — runs from inner to outer (context engine first, summarization last)
4. For each tool call: each middleware's `awrap_tool_call` fires in order
5. After each tool returns: each middleware's `awrap_after_tool` fires in order
6. Repeat from step 1 until LLM produces a final answer or budget exhausted

---

## Middleware Reference

### Summarization

**File:** `summarization.py`  
**Class:** `Summarization` (extends `SummarizationMiddleware`)  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`

Prunes conversation history when it exceeds token limits, preserving recent turns and generating a compressed summary of older messages.

**Behavior:**

- On `awrap_before_agent`: counts total tokens in the message history. If above the configured `max_tokens`, it:
  1. Keeps the last N turns of recent conversation intact
  2. Compresses everything before that into a summarization prompt
  3. Injects the summary as a `SystemMessage` at the start of the message list
- On `awrap_after_agent`: stores the summarization result into `state_register_mem`

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

### ToolCallNormalize

**File:** `tool_call_normalize.py`  
**Class:** `ToolCallNormalize` (extends `AgentMiddleware`)  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

Repairs malformed tool calls — primarily fixes **unpaired ID/name patterns** where the LLM produces tool calls with incorrect or mismatched `id`/`name` fields.

**Behavior:**

- **Pair Repair:** When a `tool_call` has multiple entries where `tool_call` `id` values don't match expected pattern, the middleware builds a mapping from name → expected id and reassigns them.
- **Deduplication:** Skips tool calls already processed (tracked via state).
- **Noise Reduction:** Strips tool call entries that fail validation.

**Why Needed:** LLMs (especially smaller or quantized models) frequently produce tool calls with dangling, swapped, or duplicated `id` fields. This middleware resolves those issues silently before they reach the runtime.

---

### ToolGuardrails

**File:** `tool_guardrails.py`  
**Class:** `ToolGuardrails` (extends `AgentMiddleware`)  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

Detects and prevents **infinite tool-call loops**, **repeated failure patterns**, and **identical retries**. Uses a three-tier escalation system.

**Tiers:**

| Tier | Condition | Action |
|---|---|---|
| `warn` | Tool name repeated N+ times across recent calls (same tool, same name, any args) | Injects a warning `SystemMessage` into the conversation before the next LLM call |
| `block` | Same tool + same arguments repeated N+ times | Prevents the tool call from executing — returns an error `ToolMessage` instead |
| `halt` | Blocked calls keep being regenerated N+ times in a row | Forces a **hard stop**: raises `AgentHalt` which terminates the agent run |

**Detection Data:**

- Tracks tool call names and serialized arguments in `state_register_mem`
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

### IterationBudget

**File:** `iteration_budget.py`  
**Class:** `IterationBudget` (extends `AgentMiddleware`)  
**Hooks:** `awrap_before_agent`, `awrap_after_tool`

Enforces a **hard cap on the number of LLM-to-tool iterations** per conversation turn. Once the limit is reached, the agent is forced to produce a final answer without further tool calls.

**Behavior:**

- On `awrap_before_agent`: checks the current iteration count against the `max_iterations` limit. If exceeded, appends a `SystemMessage` instructing the LLM to answer immediately using available information.
- On `awrap_after_tool`: increments the iteration counter in `state_register_mem`.
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

### HeartbeatStaleness

**File:** `heartbeat_staleness.py`  
**Class:** `HeartbeatStaleness` (extends `AgentMiddleware`)  
**Exported as:** `HeartbeatStaleness`  
**Used by:** Worker agents only (not the main agent)  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_model_call`, `awrap_tool_call`

Detects when a worker agent has been **idle or unresponsive** for too long and terminates it. Uses a periodic heartbeat timer that checks whether the agent has made progress (advanced iteration count or changed current tool).

**Two-Threshold System:**

| State | Threshold | Rationale |
|---|---|---|
| **Idle** (no tool running) | `stale_cycles_idle` (default 7 cycles ≈ 7 min) | Tighter — the agent is likely stuck on a hung call |
| **In-tool** (tool currently running) | `stale_cycles_in_tool` (default 20 cycles ≈ 20 min) | Looser — the tool may be legitimately long-running |

**Progress Detection:**

Every `heartbeat_interval_minutes` (default 1 min), a background timer compares the agent's current `(iteration_count, current_tool)` pair against the previously observed values. If **either** has advanced, the stale counter resets to zero; otherwise it increments by one.

**Termination:**

When the stale counter reaches the configured threshold, the session is marked as `killed`. Subsequent `awrap_model_call` or `awrap_tool_call` invocations raise **`HeartbeatTimeoutError`**, gracefully terminating the agent.

**State Storage:**

All per-session state is kept in `state_register_mem` so it survives across middleware hooks within the same turn:

| Key | Purpose |
|---|---|
| `heartbeat_iter` | Current iteration count |
| `heartbeat_tool` | Currently running tool name (or `None`) |
| `heartbeat_stale` | Consecutive stale cycles counter |
| `heartbeat_killed` | Whether the session has been terminated |

**Configuration:**

```json
{
  "heartbeat_staleness": {
    "heartbeat_interval_minutes": 1,
    "stale_cycles_idle": 7,
    "stale_cycles_in_tool": 20
  }
}
```

---

### MultimodalProcessor

**File:** `multimodal_processor.py`  
**Class:** `MultimodalProcessor` (extends `AgentMiddleware`)  
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
**Class:** `ContextEngineHook` (extends `AgentMiddleware`)  
**Hooks:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`

The **innermost middleware** — closest to the LLM. Manages system prompts, conversation persistence, periodic **nudge** interventions (memory review, skill review), and **knowledge graph maintenance**.

#### Core Behavior (`core.py`)

- **`awrap_before_agent`:**
  1. Loads the **system prompt** from cache (`MemoryCache`), with fallback to a default
  2. **Sets thread-safe variables** (`conversation_id`, `user_id`) for downstream hooks
  3. Loads any saved messages from `MesMemory` and merges them into the conversation state
  4. Injects the (optionally customized) system prompt as the first `SystemMessage`
- **`awrap_after_agent`:**
  1. Persists the assistant response to `MesMemory` via `add_messages()`
  2. Runs the **nudge system** (see below) as a post-agent side effect
  3. Calls **`xp_graph.after_turn(session_id)`** for periodic knowledge graph maintenance (e.g., pruning stale nodes, updating edge weights). Wrapped in try/except — failure is non-fatal and logged at debug level.
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

#### xp_graph Integration

After the nudge logic, `aafter_agent` calls `xp_graph.after_turn(session_id)` for periodic knowledge graph maintenance. This includes tasks such as pruning stale nodes and updating edge weights. The call is wrapped in a try/except block — if it fails, the error is logged at debug level and does not interrupt the agent flow.

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
- **`xp_graph`** — knowledge graph module for periodic maintenance after each turn
- **`system/config.tool.md`** — configuration file for system prompt and nudge intervals

---

## Shared State System

All middleware access two shared singleton instances from `runtime.state_register`:

| Instance | Class | Persistence | Purpose |
|---|---|---|---|
| `state_register_mem` | `StateRegisterMeM` | In-memory, per-session | Counters, flags, current summaries, window buffers, heartbeat state |
| `state_register_db` | `StateRegisterDB` | SQLite-backed, per-session | Structured records surviving process restarts |

Both classes inherit from `Register` and expose the same interface:

| Method | Description |
|---|---|
| `set_state(session_id, key, value)` | Set a key-value pair for a session |
| `get_state(session_id, key, default)` | Get a value for a key, with default fallback |
| `get_all_states(session_id)` | Get all key-value pairs for a session |
| `delete_state(session_id, key)` | Delete a specific key |
| `clear_session(session_id)` | Remove all state for a session |
| `has_session(session_id)` | Check if a session exists |
| `has_key(session_id, key)` | Check if a key exists for a session |
| `update_states(session_id, states)` | Bulk update multiple keys |

### Initialization Guard

Both `StateRegisterMeM` and `StateRegisterDB` use an `_initialized` guard in `__init__` to prevent re-initialization:

```python
class StateRegisterMeM(Register):
    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._states = {}
        self._initialized = True
```

This fixes a bug where `Register.clear_all_register_sessions` could trigger `__init__` and reset `_states`, wiping all in-memory state.

### Namespace Convention

Each middleware uses its own top-level key namespace:

```
state_register_mem (session "abc123") = {
    "summarization": { "current_summary": "..." },
    "tool_call_normalize": { "last_names": [...] },
    "tool_guardrails": { "tool_calls": [...], "block_count": 3 },
    "iteration_budget": { "count": 5 },
    "heartbeat_iter": 3,
    "heartbeat_tool": "web_search",
    "heartbeat_stale": 0,
    "heartbeat_killed": False,
    "multimodal_processor": { "resolved_uris": [...] },
}
```

---

## Configuration

Middleware is configured in the main agent builder. The chain order and parameters are set during agent construction, not via a separate config file.

### Example Builder Configuration

```python
from agent.middlewares import (
    Summarization,
    ToolCallNormalize,
    ToolGuardrails,
    IterationBudget,
    HeartbeatStaleness,
    MultimodalProcessor,
    ContextEngineHook,
)

middlewares = [
    Summarization(session_id="session_001"),
    ToolCallNormalize(session_id="session_001"),
    ToolGuardrails(config=ToolCallGuardrailConfig(warn_threshold=4, block_threshold=3, halt_threshold=3)),
    IterationBudget(session_id="session_001", max_iterations=10),
    # HeartbeatStaleness — worker agents only
    MultimodalProcessor(session_id="session_001"),
    ContextEngineHook(session_id="session_001"),
]
```

### Per-Middleware Parameters

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
  iteration_budget:
    max_iterations: 10
  heartbeat_staleness:
    heartbeat_interval_minutes: 1
    stale_cycles_idle: 7
    stale_cycles_in_tool: 20
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
IterationBudget.awrap_before_agent(state)
    │  inject "answer immediately" if over budget
    ▼
HeartbeatStaleness.awrap_before_agent(state)  [worker agents only]
    │  reset counters, start heartbeat timer
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
    │  persist to MesMemory, run nudge, xp_graph.after_turn()
    ▼
MultimodalProcessor.awrap_after_agent(state)
    │  clean up resolved URIs from history
    ▼
HeartbeatStaleness.awrap_after_agent(state)  [worker agents only]
    │  stop heartbeat timer
    ▼
IterationBudget.awrap_after_agent(state)
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
    ├─ IterationBudget.awrap_tool_call(state, tc)
    │     (no-op typically)
    ├─ HeartbeatStaleness.awrap_tool_call(state, tc)  [worker agents only]
    │     track current tool, raise HeartbeatTimeoutError if killed
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

Register it in the agent builder:

```python
middlewares = [
    # ...existing middlewares...
    MyCustomMiddleware(config={"my_param": "value"}),
]
```

---

## Appendix

### File Layout

```
agent/middlewares/
├── __init__.py                   # Public exports
├── summarization.py              # Summarization
├── tool_call_normalize.py        # ToolCallNormalize
├── tool_guardrails.py            # ToolGuardrails
├── iteration_budget.py           # IterationBudget
├── heartbeat_staleness.py        # HeartbeatStaleness
├── multimodal_processor.py       # MultimodalProcessor
├── context_engine/
│   ├── __init__.py
│   ├── core.py                   # ContextEngineHook (main)
│   └── nudge.py                  # Nudge logic (memory/skill review)
├── README.md                     # This file
└── README.zh.md                  # Chinese version
```

### Exports (`__init__.py`)

```python
from .summarization import Summarization
from .tool_guardrails import ToolGuardrails
from .iteration_budget import IterationBudget
from .context_engine import ContextEngineHook
from .tool_call_normalize import ToolCallNormalize
from .heartbeat_staleness import HeartbeatStaleness
from .multimodal_processor import MultimodalProcessor
```
