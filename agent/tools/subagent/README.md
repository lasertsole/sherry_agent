[**中文文档**](README.zh.md) | **English**

---

# Subagent System

> A hierarchical task decomposition and parallel execution subsystem with experience knowledge graph integration.

## Overview

The **Subagent System** enables the AI Agent to decompose complex tasks, execute sub-tasks in parallel in the background, and return results asynchronously through a message bus. It features an **experience knowledge graph (xp_graph) closed loop**: draft → distill → ingest → recall → assemble.

Core layers:

- **`SubagentManager`** — Singleton orchestrator that manages the lifecycle of background subagent tasks.
- **`Commander`** — Per-task LangGraph agent that plans, decomposes, and dispatches work to workers.
- **Distiller** — Post-task distillation engine that extracts reusable knowledge into the xp_graph.
- **Draft Tool** — Agent-callable tool for recording key findings during task execution.

## Architecture

```
User / Main Agent
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│                     SubagentManager                          │
│  (singleton, lifecycle orchestrator)                         │
│                                                              │
│  _run_subagent() flow:                                      │
│    1. Recall xp_graph → inject AIMessage into Commander     │
│    2. Commander executes task (tools: todo_writer, worker,  │
│       draft)                                                 │
│    3. Publish result to bus (Plan C)                        │
│    4. Distill experiences into knowledge graph               │
│    5. Clear runtime registers                               │
└──────────────────────────────────────────────────────────────┘
       │ creates
       ▼
┌──────────────────────────────────────────────────────────────┐
│                      Commander Agent                         │
│  (LangGraph, per-task instance)                             │
│                                                              │
│  Tools:                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                 │
│  │TodoWriter│  │  Worker  │  │  Draft   │                  │
│  │(write    │  │(parallel │  │(record   │                  │
│  │ todo.md) │  │ dispatch)│  │findings) │                  │
│  └──────────┘  └────┬─────┘  └──────────┘                 │
│                      │                                       │
│  Middlewares:        │                                       │
│  ┌───────────────┐   │                                       │
│  │Summarization  │   │                                       │
│  ├───────────────┤   │                                       │
│  │TODOManager    │   │                                       │
│  │(inject+clean)│   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolCallNorm   │   │                                       │
│  ├───────────────┤   │                                       │
│  │IterationBudget│   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolGuardrails │   │                                       │
│  └───────────────┘   │                                       │
└──────────────────────┼──────────────────────────────────────┘
                        │ dispatches
                        ▼
                ┌────────────────┐
                │ Worker Agent   │
                │ (codeact_agent)│
                │ Worker Agent   │
                │ ... (parallel) │
                └────────────────┘
                        │
                        ▼ after task
┌──────────────────────────────────────────────────────────────┐
│              Experience Knowledge Graph (xp_graph)           │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  Draft Tool │→ │  Distiller  │→ │  xp_graph   │         │
│  │(record notes│  │(auxiliary   │  │(nodes/edges │         │
│  │ during task)│  │  LLM extract│  │ vector/FTS5)│         │
│  └─────────────┘  └─────────────┘  └──────┬──────┘        │
│                                              │ recall        │
│  ┌───────────────────────────────────────────┘               │
│  │  Next task: recall → assemble → inject as AIMessage      │
│  └───────────────────────────────────────────────────────────│
│                                                              │
│  DB Roles:                                                   │
│    default → store/xp_graph/xp_graph.db (strategy-level)    │
│    worker  → store/xp_graph/worker/xp_graph.db (operation)  │
└──────────────────────────────────────────────────────────────┘
```

## Module Structure

```
subagent/
├── __init__.py              # Exports: build_subagent_tool
├── base.py                  # SubagentManager — singleton orchestrator + distillation
├── core.py                  # @tool subagent_tool — async spawn interface
├── type.py                  # SubAgentOutput — pydantic data model
├── draft.py                 # Draft @tool — record key findings + helper functions
├── distiller.py             # Distiller — post-task experience distillation
├── commander/
│   ├── __init__.py          # Exports: build_commander
│   ├── core.py              # build_commander() — creates LangGraph agent
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — writes todo.md files
│   │   └── worker/
│   │       ├── core.py      # Worker — parallel sub-task dispatch
│   │       └── middlewares/
│   │           └── WorkerSummarization.py
│   └── middlewares/
│       └── core.py          # TODOManager — inject + archive todo context
├── templates/
│   └── subagent_announce.md # Jinja2 template for result announcement
├── README.md
└── README.zh.md
```

## Experience Knowledge Graph Closed Loop

### Flow

```
1. Task Execution:  Commander/Worker call draft_tool → state_register_db
2. Task Completion:  bus.publish → distill_and_ingest → Register.clear_all
3. Distillation:     auxiliary_llm extracts nodes/edges from drafts + result
4. Ingestion:        strategy → xp_graph("default"), operation → xp_graph("worker")
5. Next Task:        recall(task) → assemble_context → AIMessage injection
```

### Draft Tool

`draft` is a `@tool` function available to Commander, Worker, and main Agent:

```python
@tool
def draft(
    key_points: str,
    category: Literal["strategy", "obstacle", "tool_pattern", "insight"],
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

Helper functions (used by distiller):
- `get_drafts(session_id)` — read all draft entries
- `append_drafts(session_id, drafts)` — merge drafts from Worker to Commander session
- `clear_drafts(session_id)` — clear draft entries after distillation

### Distiller

`distill_and_ingest()` runs after each subagent task (Plan C order):

1. **Strategy distillation** → `get_instance("default").ingest_experiences()` (Commander-level patterns)
2. **Operation distillation** → `get_instance("worker").ingest_experiences()` (Worker-level techniques)

Worker drafts are merged into the Commander session before distillation.

### Knowledge Graph Injection

Before `agent.ainvoke()`, recalled experiences are injected as an `AIMessage`:

```python
messages = [HumanMessage(content=task)]
# recall from xp_graph
if recall_result["nodes"]:
    assembled = assemble_context(db, nodes, edges)
    messages.append(AIMessage(content=f"徊\n{system_prompt}\n\n{xml}\n徊"))
```

- **Commander**: recalls from `xp_graph("default")` (strategy-level)
- **Worker**: recalls from `xp_graph("worker")` (operation-level)

## Data Model

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]
    finish_reason: str
    result: str
```

## SubagentManager Lifecycle

### Plan C: Publish → Distill → Clear

After Commander execution completes (success, timeout, or error):

```
1. Publish result to bus (user gets notification immediately)
2. distill_and_ingest() (drafts are still in state_register_db)
3. Register.clear_all_register_sessions() (cleanup, drafts cleared)
```

This ensures the user receives results promptly while drafts remain available for distillation.

### Spawn → Execute → Announce

```
spawn(task, session_id)
  │
  ├─ Generate task_id (timestamp-based)
  ├─ Create asyncio task (_run_subagent)
  ├─ Track in _running_tasks and _session_tasks
  ├─ Register _cleanup callback
  └─ Return "started" message

_run_subagent(session_id, task_id, task, label)
  │
  ├─ Recall commander xp_graph → build messages with AIMessage injection
  ├─ Build Commander agent
  ├─ agent.ainvoke({messages: [HumanMessage(task), AIMessage(knowledge)]})
  ├─ Render announcement template with SubAgentOutput
  ├─ Publish InboundMessage to bus
  ├─ distill_and_ingest() → extract experiences into knowledge graph
  └─ Register.clear_all_register_sessions()
```

### Service Mode

`start_service()` launches `_consume_loop()` which:
1. Awaits `InboundMessage` from the bus.
2. Re-personalizes the result through the character persona.
3. Forwards to the registered `_consumer` callback.

## Commander Agent

### Construction

`build_commander()` builds a LangGraph agent with:

| Component | Details |
|-----------|---------|
| **System Prompt** | Task decomposition, parallelization, dynamic plan adjustment, draft recording |
| **Model** | `main_llm` (shared project model) |
| **Checkpointer** | `InMemorySaver` |
| **Tools** | `todo_writer` + `worker` + `draft` |
| **Middlewares** | `SummarizationMiddleware` (trigger 15, keep 8) + `TODOManager` + `ToolCallNormalize` + `IterationBudget` + `ToolGuardrails` |
| **Response Format** | `SubAgentOutput` structured output |

## Commander Middlewares

### TODOManager (replaces TodoInjector + TodoCleaner)

- **`abefore_model`**: Reads `todo/{task_id}.md` and injects as `[SYSTEM CONTEXT - TODO LIST UPDATE]`.
- **`aafter_agent`**: Archives todo file to `todo_archive/` or deletes it.

### ToolCallNormalize

Fixes orphan tool calls after summarization trims messages.

### IterationBudget

Limits the number of agent iterations per task.

### ToolGuardrails

Validates tool calls against safety rules.

## Worker Agent

Workers are `codeact_agent` instances (not LangGraph agents) with:

- **Tools**: `build_worker_tools()` (all tools except subagent-specific ones, including `draft`)
- **Middlewares**: `WorkerSummarization` + `HeartbeatStaleness` + `IterationBudget`
- **Response Format**: `SubAgentOutput`
- **xp_graph injection**: Recalls from `xp_graph("worker")` before execution
- **Draft merge**: Worker drafts are merged into Commander session in `finally` block

## FAQ

### Why is distiller moved out of xp_graph?

`distiller.py` was originally inside `xp_graph/extractor/`, but it imports `draft.py` (subagent layer). This created a reverse dependency: `xp_graph` (infrastructure) → `subagent` (business). Moving distiller to `subagent/` makes the dependency direction one-way: `subagent/distiller` → `xp_graph` ✓

### Why Plan C (publish → distill → clear)?

The user should receive results immediately. Distillation needs draft data from `state_register_db`, which would be lost if `Register.clear_all` runs first. Plan C ensures both: prompt delivery and complete distillation.

### What if distillation fails?

Distillation is wrapped in `try/except`. Failure only logs a warning — it does not affect the result already published to the user.

### How are Worker drafts collected?

In `_arun_task`'s `finally` block, Worker drafts are read via `get_drafts(worker_session_id)` and appended to the Commander session via `append_drafts(commander_session_id, ...)`. The distiller reads from the Commander session uniformly.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent Framework | LangGraph (`CompiledStateGraph`) + codeact_agent |
| LLM | `main_llm` (shared), `auxiliary_llm` (distillation) |
| Checkpointing | `InMemorySaver` |
| Middleware | `@before_model` / `@after_agent` decorators |
| Knowledge Graph | `xp_graph` (SQLite + FTS5 + vector search + PageRank) |
| Async | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| Data Validation | Pydantic v2 |
| Templating | Custom `render_template_file()` (Jinja2-style) |
| Message Bus | Project-internal `MessageBus` / `InboundMessage` |
| State Management | `state_register_db` (SQLite), `state_register_mem` (in-memory) |
