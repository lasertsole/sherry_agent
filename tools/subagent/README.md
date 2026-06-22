[**中文文档**](README.zh.md) | **English**

---

# Subagent System

> A hierarchical task decomposition and parallel execution subsystem for the EMA AI Agent.

## Overview

The **Subagent System** enables the EMA AI Agent to decompose complex tasks, execute sub-tasks in parallel in the background, and return results asynchronously through a message bus. It consists of two core layers:

- **`SubagentManager`** — Singleton orchestrator that manages the lifecycle of background subagent tasks.
- **`Commander`** — Per-task LangGraph agent that plans, decomposes, and dispatches work to sub-subagents.

## Architecture

```
User / Main Agent
       │
       ▼
┌──────────────────────────────────────┐
│          SubagentManager             │
│  (singleton, lifecycle orchestrator) │
│                                      │
│  - spawn() → creates background task │
│  - _run_subagent() → builds & runs   │
│  - cancel_by_session() → cleanup     │
│  - start_service() → event loop      │
│  - _consume_loop() → relay results   │
└──────────┬───────────────────────────┘
           │ creates
           ▼
┌──────────────────────────────────────┐
│           Commander Agent            │
│  (LangGraph, per-task instance)      │
│                                      │
│  Tools:                              │
│  ┌──────────┐  ┌──────────┐         │
│  │TodoWriter│  │  Worker  │         │
│  │(write    │  │(parallel │         │
│  │ todo.md) │  │ dispatch)│         │
│  └──────────┘  └────┬─────┘         │
│                     │                │
│  Middlewares:       │                │
│  ┌──────────────┐   │                │
│  │TodoInjector  │   │                │
│  │(before model)│   │                │
│  ├──────────────┤   │                │
│  │Summarization │   │                │
│  │Middlewar     │   │                │
│  ├──────────────┤   │                │
│  │TodoCleaner   │   │                │
│  │(after agent) │   │                │
│  └──────────────┘   │                │
└─────────────────────┼────────────────┘
                      │ dispatches
                      ▼
              ┌────────────────┐
              │ Sub-Subagent   │
              │ sub-subagent   │
              │ sub-subagent   │
              │ ... (parallel) │
              └────────────────┘
```

## Module Structure

```
subagent/
├── __init__.py              # Exports: SubagentManager, subagent_manager
├── core.py                  # SubagentManager — singleton orchestrator
├── type.py                  # SubAgentOutput — pydantic data model
├── commander/
│   ├── __init__.py          # Exports: build_commander
│   ├── core.py              # build_commander() — creates LangGraph agent
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — writes todo.md files
│   │   └── worker.py        # Worker — parallel sub-subagent dispatch
│   └── middlewares/
│       ├── __init__.py      # Exports: todo_injector_builder, todo_cleaner_builder
│       ├── todo_injector.py # Pre-model middleware — injects todo status
│       └── todo_cleaner.py  # Post-agent middleware — archives/deletes todo files
├── templates/
│   └── subagent_announce.md # Jinja2 template for result announcement
├── README.md                # Entry point
├── README.en.md             # This file (English)
└── README.zh.md             # Chinese version
```

## Data Model

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]         # Task success/failure
    finish_reason: str                      # Completion reason (error details if failed)
    result: str                             # Output or result storage path
```

## SubagentManager Lifecycle

### Singleton Pattern

`SubagentManager` uses a classic singleton (`__new__` + `_instance` guard). Every `SubagentManager()` call returns the same instance. The `_initialized` flag prevents re-initialization.

### Event Loop Management

On construction:
1. Tries `asyncio.get_running_loop()` — reuses the current event loop if one is running.
2. Falls back to `asyncio.new_event_loop()` — creates a dedicated loop for background tasks.

### Spawn → Execute → Announce

```
spawn(task, session_id)
  │
  ├─ Generate task_id (UUID, first 8 chars)
  ├─ Create asyncio.create_task(_run_subagent(...))
  ├─ Track in _running_tasks and _session_tasks
  ├─ Register _cleanup callback (removes from tracking on completion)
  └─ Return "started" message to caller

_run_subagent(session_id, task_id, task, label)
  │
  ├─ Build Commander agent via build_commander(session_id, task_id)
  ├─ agent.ainvoke({messages: [HumanMessage(task)]})
  │     └─ Commander decomposes, calls tools, returns SubAgentOutput
  ├─ Render subagent_announce.md template with result
  ├─ Create InboundMessage (channel="system", metadata injected_event="subagent_result")
  └─ Publish to MessageBus → consumer relays to user
```

### Cancellation

`cancel_by_session(session_id)` — cancels all running background tasks for a given session and waits for graceful shutdown via `asyncio.gather(return_exceptions=True)`.

### Service Mode

`start_service()` launches `_consume_loop()` which:
1. Awaits `InboundMessage` from the bus.
2. Re-personalizes the result through the character persona (system prompt + chat model).
3. Forwards to the registered `_consumer` callback.

## Commander Agent

### Construction

`build_commander(session_id, task_id)` — ensures `{SESSIONS_DIR}/{session_id}/todo/` exists, then builds a LangGraph agent with:

| Component | Details |
|-----------|---------|
| **System Prompt** | Comprehensive guidance on task decomposition, parallelization rules, todo format, and dynamic plan adjustment |
| **Model** | `main_llm` (shared across the agent system) |
| **Checkpointer** | `InMemorySaver` — preserves conversation state within the session |
| **Tools** | `todo_writer` + `worker` |
| **Middlewares** | `SummarizationMiddleware` (trigger at 15 messages, keep 8) + `todo_injector` (pre-model) + `todo_cleaner` (post-agent) |
| **Response Format** | `SubAgentOutput` structured output |

### System Prompt Highlights

The Commander's persona is an "intelligent task commander" that:

1. **Assesses complexity** — Simple tasks → execute directly. Complex tasks → proceed with todo workflow.
2. **Decomposes** — Breaks work into subtasks with priority, parallel group assignment, and clear descriptions.
3. **Parallelizes** — Groups independent subtasks into single `worker` calls for concurrent execution.
4. **Tracks** — Maintains a todo.md file with status, results, and progress stats.
5. **Adapts** — Supports minor adjustments (modify tasks) and major rework (rewrite plan) based on execution feedback.
6. **Handles failures** — Logs failures, decides retry/skip/replan.

## Commander Tools

### TodoWriter (`write_todo`)

- **Purpose**: Writes/updates `todo/{task_id}.md` in the session's todo directory.
- **Behavior**: Overwrites the file with the full content every call.
- **Sync + Async**: Supports both `_run` (sync) and `_arun` (async).

### Worker (`worker`)

- **Purpose**: Executes multiple independent sub-tasks concurrently.
- **Input**: `WorkerArgs.worker_tasks: list[WorkerTask]`
  - Each `WorkerTask` has: `label`, `description`, `timeout_mins` (5-30, default 5).
- **Execution Model**:
  - Creates one `asyncio.create_task` per subtask.
  - Runs all via `asyncio.gather`.
  - Each subtask agent is a full LangGraph agent with:
    - Context Engine integration (`assemble()` for memory, `after_turn()` for experience extraction).
    - `build_core_tools()` — all available tools except the commander's own tools.
    - `SummarizationMiddleware` (trigger at 20, keep 10).
    - `SubAgentOutput` response format.
    - Configurable timeout via `asyncio.wait_for`.
- **Result**: Each subtask returns an announcement string rendered from `subagent_announce.md`.

## Commander Middlewares

### TodoInjector (pre-model)

- **Hook**: `@before_model` — runs before every model invocation.
- **Function**: Reads `todo/{task_id}.md` and injects its content as a `HumanMessage` with tag `[SYSTEM CONTEXT - TODO LIST UPDATE]`.
- **Skip**: If the todo file doesn't exist or can't be read, returns `None` (no-op).

### TodoCleaner (post-agent)

- **Hook**: `@after_agent` — runs after the agent completes.
- **Function**: Cleans up the `todo/{task_id}.md` file.
- **Modes**:
  - `"delete"` — Directly removes the file via `os.remove()`.
  - `"archive"` (default) — Moves to `todo_archive/{task_id}_{timestamp}.md` via `shutil.move()`.

### SummarizationMiddleware

- **Trigger**: When message count exceeds 15.
- **Keep**: Reduces to 8 most recent messages.
- **Model**: Uses the same `main_llm` for summarization.

## SubagentTool (External Interface)

Located in `tools/subagent.py` — a LangChain `BaseTool` that allows the main Agent to spawn subagents:

```python
class SubagentTool(BaseTool):
    name = "subagent"
    description = "Spawn a subagent for background task execution."

    async def _arun(self, task: str, label: str | None = None) -> str
```

- **Async-only**: `_run()` raises `RuntimeError` to prevent deadlocks from synchronous calls.
- **Thread-safe**: Uses `asyncio.run_coroutine_threadsafe()` to schedule work on the SubagentManager's event loop.
- **Requires running event loop**: Checks `event_loop.is_running()` before spawning.

## Announcement Template

`templates/subagent_announce.md` is a Jinja2-style template rendered with:

```markdown
[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}
finish_reason: {{ finish_reason }}
Result: {{ result }}

Summarize this naturally for the user. Keep it brief (1-2 sentences).
Do not mention technical details like "subagent" or task IDs.
```

## Task Lifecycle Diagram

```
User Task Request
       │
       ▼
Main Agent calls SubagentTool._arun()
       │
       ▼
SubagentManager.spawn()
  ├── Generate task_id
  ├── Create asyncio task (_run_subagent)
  └── Return "started" to caller
       │
       ▼
Commander Agent (LangGraph)
  ├── Step 0: Assess complexity
  ├── Step 1: Write todo.md (TodoWriter)
  ├── Step 2: Execute parallel groups (Worker)
  │     └── Sub-subagent 1 ──► result
  │     └── Sub-subagent 2 ──► result
  │     └── Sub-subagent 3 ──► result
  ├── Step 3: Handle dependencies (if any)
  ├── Step 4: Update todo.md
  └── Return SubAgentOutput
       │
       ▼
SubagentManager._run_subagent()
  ├── Render announcement template
  ├── Create InboundMessage on bus
  └── Consumer → persona-style relay to user
```

## FAQ

### Why is SubagentManager a singleton?
Background tasks must be tracked globally, not per-session. Singleton ensures a single point of control for cancellation, lifecycle management, and the event loop.

### Why async-only for SubagentTool?
The main agent may run in a different thread. Synchronous invocation would block the calling thread and risk deadlocks. `asyncio.run_coroutine_threadsafe()` provides thread-safe scheduling.

### What happens if a sub-subagent times out?
The `Worker` tool wraps each subtask in `asyncio.wait_for()`. On timeout, a failure announcement is rendered with the timeout duration.

### How does the Commander know what to do next?
The `TodoInjector` middleware reads `todo.md` before every model call and injects it as context, so the Commander always sees the current plan status.

### Can I customize the Commander's behavior?
Yes — the system prompt in `commander/core.py` is the primary control surface. Modify the prompt to change decomposition strategy, parallelization rules, or todo format.

### What happens to failed sub-subagents?
The Commander decides: retry, skip, or replan. The failure is recorded in the `finish_reason` field and visible in the todo.md update.

### How are results delivered to the user?
Results pass through the `MessageBus` as an `InboundMessage` with `injected_event: "subagent_result"`. The `_consume_loop` re-personalizes the message through the character persona before displaying.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent Framework | [LangGraph](https://github.com/langchain-ai/langgraph) (`CompiledStateGraph`) |
| LLM | `main_llm` (shared project model, configurable via `.env`) |
| Checkpointing | `InMemorySaver` (memory-based, within session) |
| Middleware | `@before_model` / `@after_agent` decorators (`langchain.agents.middleware`) |
| Async | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| Data Validation | Pydantic v2 (`BaseModel`, `Field`, `Literal`) |
| Templating | Custom `render_template_file()` (Jinja2-style) |
| Message Bus | Project-internal `MessageBus` / `InboundMessage` |
| Memory | Context Engine (`assemble()` / `after_turn()`) |
