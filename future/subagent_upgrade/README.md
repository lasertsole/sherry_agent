[**中文文档**](README.zh.md) | **English**

---

# Subagent System

> A hierarchical task decomposition and parallel execution subsystem for the EMA AI Agent.

## Overview

The **Subagent System** enables the EMA AI Agent to decompose complex tasks, execute sub-tasks in parallel in the background, and return results asynchronously through a message bus. It consists of two core layers:

- **`SubagentManager`** — Singleton orchestrator that manages the lifecycle of background subagent tasks.
- **`Commander`** — Per-task LangGraph agent that plans, decomposes, and dispatches work using a programmatic orchestration approach.

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
│  ┌──────────────┐  ┌──────────────┐ │
│  │  TodoWriter  │  │    Worker    │ │
│  │(todo.md mgmt)│  │(parallel exec)│ │
│  └──────┬───────┘  └──────┬───────┘ │
│         │                 │          │
│  ┌──────┴─────────────────┴───────┐ │
│  │   Program Orchestration Tools   │ │
│  │  - program_generator            │ │
│  │  - program_runner               │ │
│  │  - program_interrupter          │ │
│  │  - program_resumer              │ │
│  └─────────────────────────────────┘ │
│                                      │
│  Middlewares:                        │
│  ┌──────────────┐  ┌──────────────┐ │
│  │TodoInjector  │  │TodoCleaner   │ │
│  │(before model)│  │(after agent) │ │
│  └──────────────┘  └──────────────┘ │
└──────────────────────────────────────┘
```

## Module Structure

```
subagent/
├── __init__.py              # Exports: SubagentManager, subagent_manager
├── core.py                  # SubagentManager — singleton orchestrator
├── type.py                  # Data models: SubAgentOutput, ProgramExecutionResult, RecoveryResult
├── commander/
│   ├── __init__.py          # Exports: build_commander
│   ├── core.py              # build_commander() — creates LangGraph agent
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — writes todo.md files
│   │   ├── worker.py        # Worker — parallel sub-subagent dispatch
│   │   ├── program_generator.py   # ProgramGenerator — generates execution programs
│   │   ├── program_runner.py      # ProgramRunner — executes programs
│   │   ├── program_interrupter.py # ProgramInterrupter — interrupts execution
│   │   ├── program_resumer.py     # ProgramResumer — resumes execution
│   │   ├── cache_manager.py       # CacheManager — manages task cache
│   │   ├── state_manager.py       # StateManager — manages execution state
│   │   └── worker_executor.py     # WorkerExecutor — executes worker tasks
│   └── middlewares/
│       ├── __init__.py      # Exports: todo_injector_builder, todo_cleaner_builder
│       ├── todo_injector.py # Pre-model middleware — injects todo status
│       └── todo_cleaner.py  # Post-agent middleware — archives/deletes todo files
├── templates/
│   └── subagent_announce.md # Jinja2 template for result announcement
├── README.md                # Entry point
└── README.zh.md             # Chinese version
```

## Data Models

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]         # Task success/failure
    finish_reason: str                      # Completion reason (error details if failed)
    result: str                             # Output or result storage path
```

### `ProgramExecutionResult`

```python
class ProgramExecutionResult(BaseModel):
    status: Literal["completed", "failed", "interrupted", "timeout", "error"]
    strategy_needed: Literal["fast_retry", "gentle_retry", "full_reset", None]
    failed_tasks: list[dict]
    completed_tasks: list[str]
    can_resume: bool
    recommendation: str
    stage: str
```

### `RecoveryResult`

```python
class RecoveryResult(BaseModel):
    status: Literal["resumed", "retrying", "reset", "error", "no_failed_tasks"]
    strategy: str
    failed_tasks: list[str]
    message: str
    can_resume: bool
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
| **System Prompt** | Comprehensive guidance on task decomposition, program orchestration, todo format, and recovery strategies |
| **Model** | `chat_model` (shared across the agent system) |
| **Checkpointer** | `InMemorySaver` — preserves conversation state within the session |
| **Tools** | `todo_writer` + `worker` + `program_generator` + `program_runner` + `program_interrupter` + `program_resumer` |
| **Middlewares** | `SummarizationMiddleware` (trigger at 15 messages, keep 8) + `todo_injector` (pre-model) + `todo_cleaner` (post-agent) |
| **Response Format** | `SubAgentOutput` structured output |

### System Prompt Highlights

The Commander's persona is an "intelligent task commander" that:

1. **Assesses complexity** — Simple tasks → use worker directly. Complex tasks → use program orchestration workflow.
2. **Decomposes** — Breaks work into subtasks with priority, parallel group assignment, and clear descriptions.
3. **Programmatic Orchestration** — Instead of directly interacting with workers, generates Python executable programs that orchestrate multiple workers with:
   - Parallel execution using asyncio.TaskGroup
   - Sequential execution wrapped in try-catch
   - Individual retry for failed parallel tasks
   - Blocking behavior for failed sequential tasks
4. **Tracks** — Maintains a todo.md file with status, results, and progress stats.
5. **Multi-level Recovery** — Supports different recovery strategies:
   - fast_retry: Retry failed tasks without changes (for timeout/network errors)
   - gentle_retry: Retry with adjusted task descriptions (for semantic/permission errors)
   - full_reset: Clear all states and caches, restart from beginning
6. **Handles interruptions** — Supports graceful interruption and state recovery.

## Commander Tools

### TodoWriter (`write_todo`)

- **Purpose**: Writes/updates `todo/{task_id}.md` in the session's todo directory.
- **Behavior**: Overwrites the file with the full content every call.
- **Sync + Async**: Supports both `_run` (sync) and `_arun` (async).

### Worker (`worker`)

- **Purpose**: Execute multiple independent subtasks concurrently (legacy simple tasks).
- **Input**: `WorkerArgs.worker_tasks: list[WorkerTask]`
  - Each `WorkerTask` has: `label`, `description`, `timeout_mins` (5-30, default 5).
- **Execution Model**:
  - Creates one `asyncio.create_task` per subtask.
  - Runs all via `asyncio.gather`.
  - Each subtask agent is a full LangGraph agent with:
    - Context Engine integration (`assemble()` for memory, `after_turn()` for experience extraction).
    - `build_core_tools()` — all available tools.
    - `SummarizationMiddleware` (trigger at 20, keep 10).
    - `SubAgentOutput` response format.
    - Configurable timeout via `asyncio.wait_for`.
- **Result**: Each subtask returns an announcement string rendered from `subagent_announce.md`.

### ProgramGenerator (`program_generator`)

- **Purpose**: Generate Python executable program from todo list.
- **Key Feature**: Creates a program that orchestrates multiple worker agents with:
  - Parallel execution using asyncio.TaskGroup
  - Sequential execution wrapped in try-catch
  - Individual retry for failed parallel tasks
  - Blocking behavior for failed sequential tasks
  - Success: `print(f"SUCCESS: {label}")`
  - Failure: `print(f"FAILED: {label} - {error}")`
  - Cache hit: `print(f"CACHED: {label}")`
- **Output**: Program file path saved to `todo/{task_id}_program.py`

### ProgramRunner (`program_runner`)

- **Purpose**: Execute the generated Python program and parse output.
- **Output Format**:
  - status: "completed" | "failed" | "interrupted"
  - strategy_needed: "fast_retry" | "gentle_retry" | "full_reset" | None
  - failed_tasks: [{"label": "...", "error": "..."}]
  - completed_tasks: ["...", "..."]
  - can_resume: true/false
  - recommendation: "..."

### ProgramInterrupter (`program_interrupter`)

- **Purpose**: Interrupt running program execution gracefully.
- **Output**: Interrupt status and save state for recovery.

### ProgramResumer (`program_resumer`)

- **Purpose**: Resume interrupted execution with different strategies.
- **Strategies**:
  - continue: Resume from last checkpoint
  - fast_retry: Retry failed tasks without changes (for timeout/network errors)
  - gentle_retry: Retry with adjusted task descriptions (for semantic/permission errors)
  - full_reset: Clear all states and caches, restart from beginning

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
- **Model**: Uses the same `chat_model` for summarization.

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
  ├── Step 2: Generate execution program (ProgramGenerator)
  ├── Step 3: Execute program (ProgramRunner)
  │     └── Worker 1 ──► SUCCESS/FAILED/CACHED
  │     └── Worker 2 ──► SUCCESS/FAILED/CACHED
  │     └── Worker 3 ──► SUCCESS/FAILED/CACHED
  ├── Step 4: Handle failures (ProgramResumer)
  ├── Step 5: Support interruption (ProgramInterrupter)
  └── Return SubAgentOutput
       │
       ▼
SubagentManager._run_subagent()
  ├── Render announcement template
  ├── Create InboundMessage on bus
  └── Consumer → persona-style relay to user
```

## Key Features

### Programmatic Orchestration
Instead of directly interacting with worker agents, the Commander generates Python executable programs that orchestrate multiple workers. This avoids context explosion when many workers return full results.

### Multi-level Recovery Strategies
- **Fast Retry**: For timeout/network errors - retry failed tasks without changes
- **Gentle Retry**: For semantic/permission errors - analyze error and adjust task description
- **Full Reset**: For continuous failures - clear all caches and states, restart from beginning

### Cache Mechanism
Successful workers are cached and won't re-execute unless the program is regenerated.

### Checkpoint & Recovery
Each stage completion is checkpointed for recovery. Users can interrupt at any time and resume later.

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
Yes — the system prompt in `commander/core.py` is the primary control surface. Modify the prompt to change decomposition strategy, orchestration rules, or recovery strategies.

### What happens to failed sub-subagents?
The Commander decides: retry (fast/gentle), skip, or full reset. The failure is recorded in the `finish_reason` field and visible in the todo.md update.

### How are results delivered to the user?
Results pass through the `MessageBus` as an `InboundMessage` with `injected_event: "subagent_result"`. The `_consume_loop` re-personalizes the message through the character persona before displaying.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent Framework | [LangGraph](https://github.com/langchain-ai/langgraph) (`CompiledStateGraph`) |
| LLM | `chat_model` (shared project model, configurable via `.env`) |
| Checkpointing | `InMemorySaver` (memory-based, within session) |
| Middleware | `@before_model` / `@after_agent` decorators (`langchain.agents.middleware`) |
| Async | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| Data Validation | Pydantic v2 (`BaseModel`, `Field`, `Literal`) |
| Templating | Custom `render_template_file()` (Jinja2-style) |
| Message Bus | Project-internal `MessageBus` / `InboundMessage` |
| Memory | Context Engine (`assemble()` / `after_turn()`) |