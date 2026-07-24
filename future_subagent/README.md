# Future Subagent — Python Subagent System

**[中文文档](./README.zh.md)** | English

> A Python implementation of a multi-level subagent system, coexisting with the existing `agent/tools/subagent/` (Commander/Worker pattern). All 7 implementation phases + robustness-plan-v3 enhancements + bug fixes + OpenClaw alignment + depth alignment + wiring fixes are complete. 203 tests pass.

## Quick Navigation

| Document | Purpose |
|----------|---------|
| [AGENTS.md](./AGENTS.md) | **Entry point** — project conventions, current progress |
| [architecture.md](./docs/architecture.md) | Overall architecture, directory structure, module dependency graph |
| [decisions.md](./docs/decisions.md) | Key technical decision records (22 decisions) |
| [integration.md](./docs/integration.md) | Integration plan with the existing system |

---

## Execution Principles

### 1. System Overview

The core goal of the Subagent system is to enable the main Agent to decompose complex tasks into parallel subtasks, dispatch them to independent child Agents for execution, and reliably deliver results back to the parent Agent upon completion. The entire system is driven by three core pipelines:

```
┌──────────────────────────────────────────────────────────────────┐
│  Parent Agent (LangGraph CompiledStateGraph)                     │
│    │                                                             │
│    ├─ 1. sessions_spawn ──► Spawn Pipeline ──► Child Agent Async │
│    │                                                             │
│    ├─ 2. sessions_yield ──► Pause current turn, await children   │
│    │                                                             │
│    ├─ 3. sessions_send  ──► A2A Bidirectional (via MessageBus)  │
│    │                                                             │
│    └─ 4. Child completes ──► Announce Pipeline ──► Deliver via  │
│                              MessageBus + Registry lifecycle     │
└──────────────────────────────────────────────────────────────────┘
```

### 2. Spawn Pipeline — Child Agent Creation & Dispatch

`spawn_subagent_direct()` is the system's entry point. When the LLM invokes the `sessions_spawn` tool, the following flow executes:

```
spawn_subagent_direct(task, requester_session_key, agent_id, mode, ...)
  │
  ├── 1. Validation Phase
  │     ├── Validate task is non-empty
  │     ├── Normalize task_name (replace non-alphanumeric with _, truncate to 64 chars)
  │     ├── Validate target_policy (agent_id in allow_agents whitelist)
  │     ├── Compute depth: parent_depth + 1, validate ≤ max_spawn_depth (default 3)
  │     ├── Validate concurrency: active children < max_children_per_agent (default 5)
  │     └── Validate runtime isolation (cross-runtime spawn blocked)
  │
  ├── 2. Role & Capability Resolution
  │     └── resolve_subagent_capabilities(depth, max_depth)
  │           ├── depth == 0       → MAIN,       control_scope=CHILDREN
  │           ├── 0 < depth < max  → ORCHESTRATOR, control_scope=CHILDREN
  │           └── depth >= max     → LEAF,        control_scope=NONE
  │
  ├── 3. Context Preparation
  │     ├── Thinking level override resolution (plan.py)
  │     ├── Attachment materialization to disk (attachments.py)
  │     │     with safety checks: path traversal, size limits, file count
  │     ├── Tool policy: DEFAULT_SUBAGENT_BLOCKED_TOOLS = [sessions_spawn,
  │     │   sessions_yield, skill_manage, memory]
  │     │   ORCHESTRATOR role auto-unblocks sessions_spawn and sessions_yield
  │     ├── Context mode: ISOLATED (empty context) or FORK (copy parent
  │     │       transcript via agent.aget_state() — Decision 9)
  │     ├── Thread binding: SESSION mode → bind_thread_for_subagent_spawn()
  │     │       creates channel thread + delivery_origin (Decision 11)
  │     ├── Runtime isolation: resolve_runtime_isolation() + cwd validation
  │     │       (Decision 15)
  │     ├── Origin routing: resolve_requester_origin_for_child()
   │     └── Scope resolution: resolve_least_privilege_scopes() by role
  │
  ├── 4. Run Registration
  │     ├── Generate child_session_key = "agent:{agent_id}:subagent:{uuid}"
  │     ├── Create SubagentRunRecord (UUID, execution=RUNNING, delivery=PENDING)
  │     ├── Store in memory dict + SQLite
  │     └── Register terminal generation (TerminalGenerationTracker)
  │
  ├── 5. Prompt Construction
  │     ├── build_subagent_system_prompt(role, task, ...)
  │     │   ├── 6-section structure: Your Role / Rules / Output Format /
  │     │   │     What You DON'T Do / Sub-Agent Spawning / Session Context
  │     │   ├── Anti-polling rule (no active status polling)
  │     │   ├── Truncation hint for output length
  │     │   ├── LEAF: structured output template from output_schema
  │     │   └── ORCHESTRATOR: "You MAY spawn further subagents via sessions_spawn."
  │     ├── Append attachment location hint to system prompt
  │     ├── Append structured output prompt from output_schema (swarm mode)
  │     └── build_subagent_initial_user_message(task, context)
  │           └── Structured envelope: [Subagent Context] / [Subagent Task] / [Subagent Additional Context]
  │
  ├── 6. Async Dispatch (Fire-and-Forget)
  │     └── asyncio.create_task(_execute_subagent(...))
  │
  └── 7. Immediate Return
        └── SpawnResult { status: "accepted", child_session_key, run_id }
```

#### Child Agent Execution

`_execute_subagent()` is a background asyncio Task responsible for the child Agent's full lifecycle:

```
_execute_subagent(run, system_prompt, user_message, forked_messages, ...)
  │
  ├── 1. Build Child Agent
  │     ├── Call build_main_tools() to get all tools
  │     ├── Filter by tool_allow/tool_deny (deny list takes precedence)
  │     ├── Build LLM: ORCHESTRATOR → build_main_llm(), LEAF → build_auxiliary_llm()
  │     ├── Create independent SQLite checkpointer
  │     └── create_agent() with five middlewares:
  │           ├── Summarization(trigger=[fraction:0.5, messages:40, tokens:30000])
  │           ├── IterationBudget(60)     — max iteration count
  │           ├── ToolGuardrails()        — tool safety guardrails
  │           ├── ToolCallNormalize()     — tool call normalization
  │           └── HeartbeatStaleness()    — heartbeat monitoring
  │
  ├── 2. Execution
  │     ├── Assemble message list: forked_messages + HumanMessage(user_message)
  │     └── await asyncio.wait_for(child_agent.ainvoke(...), timeout)
  │
  ├── 3. Result Extraction
  │     └── Extract result_text from the last message returned by ainvoke
  │
  └── 4. Finally (always executed regardless of success/failure)
        ├── TimeoutError → outcome = TIMEOUT
        ├── Exception   → outcome = ERROR
        ├── complete_run(run_id, outcome, result_text)  — update Registry
        │     └── result_text capped at 24000 chars
        └── run_subagent_announce_flow(updated_run)      — trigger Announce
```

### 3. Registry — Run State Registry

The Registry is the state hub of the entire system, managing the lifecycle of all child Agent run records.

#### Storage Architecture

```
┌─────────────────────────────────────────────────┐
│  Memory Store (registry/memory.py)              │
│  threading.Lock-protected dict[str, SubagentRunRecord]  │
│  ↓ periodic snapshot                             │
│  SQLite (registry/store_sqlite.py)              │
│  future_subagent/data/subagent_registry.db      │
│  Table: subagent_runs(run_id PK, data JSON)     │
└─────────────────────────────────────────────────┘
```

- Memory is the primary store; all read/write operations target the in-memory dict directly
- SQLite is a persistent backup, snapshotted via `periodic_persist(interval=30s)` called by Sweeper
- On startup, `init_registry()` restores existing records from SQLite
- Single-record upsert/delete sync to SQLite in real time

#### SubagentRunRecord Key Fields

| Category | Field | Description |
|----------|-------|-------------|
| **Identity** | `run_id` | UUID, unique identifier |
| | `task_run_id` | Stable ID across steer/restart |
| | `child_session_key` | `"agent:{agentId}:subagent:{uuid}"` |
| | `requester_session_key` | Parent session key |
| **Spawn Params** | `spawn_mode` | RUN (one-shot) / SESSION (persistent) |
| | `context_mode` | ISOLATED / FORK |
| | `depth` | Nesting depth |
| | `role` | MAIN / ORCHESTRATOR / LEAF |
| **Ownership** | `completion_owner_session_key` | Session key that owns completion delivery |
| | `spawned_by` | Identity that initiated spawn |
| | `spawned_cwd` | Working directory at spawn time |
| **Scoping** | `scopes` | Granted permission scopes |
| | `inherited_tool_policy_version` | Version of inherited tool policy |
| **Schema** | `output_schema` | JSON Schema for structured output validation |
| **Execution** | `execution.status` | RUNNING → INTERRUPTED → TERMINAL |
| | `execution.outcome` | OK / ERROR / TIMEOUT / UNKNOWN |
| **Delivery** | `delivery.status` | PENDING → IN_PROGRESS → DELIVERED |
| | `delivery.attempt_count` | Delivery retry count |
| **Attachments** | `attachments_dir` | Absolute path to attachment directory |
| | `attachments_root_dir` | Root dir for safe cleanup validation |

### 4. Three Core State Machines

#### 1. ExecutionState — Execution State Machine

```
    RUNNING ──────────────────► INTERRUPTED
      │                            │
      │ (completed/error/timeout)  │ (resume)
      ▼                            │
    TERMINAL ◄─────────────────────┘
      ▲
      │ (restart)
      └────────────────────────────
```

- `RUNNING`: Child Agent is executing
- `INTERRUPTED`: Paused by yield/steer
- `TERMINAL`: Final state (completed/error/timeout), irreversible

#### 2. CompletionDeliveryState — Delivery State Machine

```
    not_required ──(RUN mode skip)──► delivered

    pending ──► in_progress ──► delivered
                    │
                    ├──(failure)──► failed ──(retry)──► pending
                    │                               │
                    │     (retries exhausted + soft cap) │
                    │                               ▼
                    └──(soft cap exceeded)──► suspended ──► discarded
```

- `not_required`: SESSION mode doesn't require delivery
- `pending → in_progress → delivered`: Normal delivery path
- `failed → pending`: Exponential backoff retry (1s, 2s, 4s; max 3 attempts)
- `suspended → discarded`: Discarded after suspension exceeds limit

#### 3. CleanupState — Cleanup State Machine

```
    registered ──► cleanup_handled ──► cleanup_completed_at
```

- `resolve_deferred_cleanup_decision()` determines whether to delete the session
- cleanup="delete" AND delivery completed/discarded/not_required → delete
- Delivery suspended/failed → keep
- Attachment cleanup uses `safe_remove_attachments_dir()` with symlink traversal protection

### 5. Announce Pipeline — Result Notification & Delivery

After a child Agent completes, the Announce pipeline reliably delivers the result back to the parent Agent.

```
Child Agent execution completed
  │
  └──► run_subagent_announce_flow(run)
         │
         ├── Pre-guards
         │     ├── execution.status != TERMINAL → skip
         │     ├── completion.required == False → skip
         │     └── delivery.status == DELIVERED → skip (idempotency)
         │
          └──► deliver_subagent_announcement(run)
                │
                ├── 1. In-process Idempotency Check
                │     └── _is_already_delivered(run) → check in-memory set
                │         key = "subagent_announce:{run_id}:gen:{generation}"
                │         set cap 10K, evict oldest 5K when full
                │
                ├── 2. Hard Cap Check
                │     └── Pending descendant count ≥ hard_cap(50) → immediate SUSPENDED
                │
                ├── 3. Descendant Check
                │     └── Only deliver wake if requester has pending descendants
                │
                ├── 4. Mark IN_PROGRESS
                │
                ├── 5. Retry Loop (up to 3 attempts)
                │     ├── _do_deliver(ctx)
                │     │     ├── Build InboundMessage:
                │     │     │     channel = "system"
                │     │     │     sender_id = "subagent"
                │     │     │     chat_id = "direct"
                │     │     │     session_id = requester_session_key
                │     │     │     metadata.injected_event = "subagent_result"
                │     │     │     content = formatted result (truncated at 4K)
                │     │     └── MessageBus.publish_inbound(msg)
                │     │     fire_delivery_target_hook() → allow redirect
                │     │
                │     ├── Success → mark DELIVERED + record idempotency key → return
                │     ├── Transient failure → sleep [5s/10s/20s] → retry
                │     ├── Compaction error → sleep [1s/2s/4s/8s] → retry
                │     └── Permanent failure → no retry
               │
                ├── 6. Retries Exhausted
                │     ├── Mark FAILED
                │     └── Pending count ≥ soft_cap(25) → mark SUSPENDED
                │
                └── 7. Cleanup
                     └── cleanup="delete" → safe_remove_attachments_dir()
```

#### Delivery Message Format

```
**Subagent Result** [{label}]
Status: completed successfully / failed: {error} / timed out
Task: {task description}
Result:
{result_text, truncated at 4000 chars}
```

### 5.1 Swarm/Collect Mode (v3)

The Swarm system enables concurrent batch execution of sub-tasks with FIFO scheduling and concurrency control:

```
configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=3))
  │
  ├── reserve_swarm_run(group_id, task, requester)
  │     └── Enqueue to FIFO + set state=RESERVED
  │
  ├── activate_swarm_run(run_id)
  │     └── Dequeue + set state=ACTIVE (respect max_concurrent)
  │
  ├── complete_swarm_run(run_id, outcome)
  │     └── Set state=COMPLETED/FAILED + auto-activate next reserved
  │
  └── build_structured_output_prompt(output_schema)
        └── Generate JSON schema prompt for structured output

validate_structured_output(result_text, output_schema)
  │
  ├── Parse result_text as JSON
  ├── Check required fields exist
  ├── Validate field types against schema
  └── Return (is_valid, error_message)

SwarmGroupConfig fields: group_id, max_children_per_group (5), max_total_per_group (0=unlimited), max_concurrent (3)

reserve_swarm_run(group_id, task, requester, launch_fingerprint=None)
  │
  ├── launch_fingerprint provided → check _launch_fingerprints for idempotent hit
  └── new run → enqueue to FIFO + set state=RESERVED

_pump_lane(group_id)
  │
  ├── Check available slots against max_concurrent
  ├── Auto-activate reserved runs when slots available
  └── Trigger _on_swarm_run_started callback on activation

onStartFailure handling:
  │
  ├── Auto-fail the run (state=FAILED)
  └── Auto-activate next queued reserved run
```

### 5.2 Delivery Dual-Path Routing (v3)

Announce delivery now routes based on requester type:

```
deliver_subagent_announcement(run)
  │
  ├── Requester is subagent → _deliver_internal_injection()
  │     ├── metadata.internal = True
  │     ├── Content: "[Subagent Internal] {label}: {status}"
  │     └── No user-visible output
  │
  └── Requester is user session → _deliver_completion_message()
        ├── Full markdown format with review instruction
        ├── Content: "**[Subagent Task]** [{label}]..."
        └── "请审阅以上子 Agent 执行结果，如需进一步操作请指示。"
```

### 5.3 Generation-Guarded Lifecycle & Kill Arbitration (v3)

```
complete_subagent_run(run_id, outcome, expected_generation)
  │
  ├── TerminalGenerationTracker.is_callback_current()
  │     └── Reject stale generation callbacks
  │
  ├── _arbitrate_kill_vs_completion(run, outcome)
  │     ├── No kill_reconciliation → pass through
  │     ├── Kill + Provider OK with result → Provider wins
  │     └── Kill + other outcome → Kill wins
  │
  ├── _should_suspend_pending_final_delivery()
  │     └── cleanup="keep" + complete + ok + expects + PENDING → suspend
  │
  └── _start_announce_cleanup_flow()
        ├── SettleWakeBatch: IDLE → COMPLETING → SETTLED → DONE
        └── Deferred cleanup with generation guard
```

### 5.4 Kill Target-State Resolution & Visibility (v3)

```
resolve_kill_target_state(run) → "killable" | "finalizing" | "terminal"

kill_subagent_run_with_cascade(run_id, cascade=True)
  │
  ├── Resolve target state
  │     ├── "terminal" → return (already done)
  │     ├── "finalizing" → wait 1s, re-check
  │     └── "killable" → proceed with kill
  │
  ├── Save kill reconciliation snapshot
  ├── Cancel task + clear session queues
  ├── If cascade: recursively kill all children
  └── Wake parent if all children settled

is_subagent_run_visible_to_session(run, session_key)
  ├── controller_session_key matches → visible
  ├── requester_session_key matches → visible
  └── otherwise → not visible
```

### 6. Depth & Role System — Hierarchical Control

The Subagent system supports multi-level nesting, controlling recursive spawn capabilities through depth and role:

```
depth 0:  MAIN Agent
           ├── Can spawn child agents
           └── control_scope = CHILDREN

depth 1:  ORCHESTRATOR (if max_depth > 1)
           ├── Can continue spawning child agents
           └── control_scope = CHILDREN

depth 2:  ORCHESTRATOR (if max_depth > 2)
           ├── Can continue spawning child agents
           └── control_scope = CHILDREN

depth N:  LEAF (depth == max_spawn_depth)
           ├── Cannot spawn child agents
           └── control_scope = NONE
```

Default `max_spawn_depth = 3`, forming a three-level tree: MAIN → ORCHESTRATOR → LEAF

**Depth Calculation**: Extract parent depth from `requester_session_key`; child depth = parent depth + 1. The number of `:subagent:` occurrences in the session key format `"agent:{id}:subagent:{uuid}"` equals the depth.

**Tool Policy Coupling**:
- LEAF role is fully restricted by `DEFAULT_SUBAGENT_BLOCKED_TOOLS` (`sessions_spawn`, `sessions_yield`, `skill_manage`, `memory`), cannot invoke `sessions_spawn`
- ORCHESTRATOR role auto-unblocks `sessions_spawn` and `sessions_yield`, enabling recursive spawning
- This ensures the hard constraint on nesting depth cannot be bypassed

### 7. Attachment System

The Spawn pipeline supports passing file attachments to child agents:

```
materialize_subagent_attachments(attachments, child_workspace, ...)
  │
  ├── 1. Validation
  │     ├── File name: no path traversal, no control chars (C0+DEL), no reserved names, no duplicate names
  │     ├── Count limit: max 50 files per spawn
  │     ├── Size limit: 1MB per file, 5MB total per spawn
  │     └── mount_path sanitization: alphanumeric + ._-/
  │
  ├── 2. Write to Isolated Directory
  │     └── <childWorkspace>/.openclaw/attachments/<uuid>/
  │
  ├── 3. Generate Manifest
  │     └── .manifest.json with file names, sizes, SHA-256 hashes
  │
  └── 4. Return System Prompt Suffix
        └── "Attachments: N file(s), M bytes. Available at: .openclaw/attachments/<uuid>"
```

### 8. Background Daemon Mechanisms

#### Sweeper (Registry Scanner)

```
registry/sweeper.py — 60-second interval loop

Each sweep executes:
  1. recover_orphaned_runs()       — recover orphaned runs
  2. finalize_suspended_deliveries() — retry/discard suspended deliveries
  3. persist_runs_to_disk()        — snapshot to SQLite
```

Orphan criteria: `RUNNING` AND `started_at` is not None AND elapsed exceeds tiered threshold (cron=2h, subagent=6h, interactive=24h). Sweeper skips wedged runs.

#### Followup (Timeout Checker)

```
followup/core.py — sweeper_interval * 2 interval loop

Each check executes:
  1. Iterate all runs
  2. Find RUNNING runs exceeding run_timeout_seconds
  3. Call recover_orphaned_runs() to force-recover
```

#### Orphan Recovery

```
orphan/recovery.py — delayed scheduling per run_id

For each orphaned run:
  1. Wait delay_seconds (default 120s)
  2. Check if still live and unended
  3. reconcile_orphaned_run() → mark TERMINAL + TIMEOUT
  4. Trigger run_subagent_announce_flow() → deliver timeout result to parent Agent
```

Deduplication: Each `run_id` is scheduled for recovery at most once.

### 9. LLM Tool Interface

#### sessions_spawn — Create Child Agent

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task` | str | required | Task description |
| `task_name` | str\|None | None | Stable alias |
| `label` | str\|None | None | Display label |
| `agent_id` | str | "main" | Target Agent ID |
| `thinking` | str\|None | None | Override thinking mode |
| `mode` | str | "run" | "run" (one-shot) / "session" (persistent) |
| `cleanup` | str | "delete" | "delete" / "keep" |
| `context` | str | "isolated" | "isolated" / "fork" |
| `attachments` | list\|None | None | File attachments (name, content, encoding, mount_path) |

Returns: `"Subagent spawned: status={status}, run_id={id}, session_key={key}"`

#### sessions_yield — Pause & Wait

Signals the main Agent to end the current turn and wait for child results to arrive. This is a **signal tool** — it does not block the thread, but informs the framework that the current turn can be paused.

Returns: `"Turn yielded. You will be resumed when subagent results arrive."`

#### sessions_send — Bidirectional Communication

| Parameter | Type | Description |
|-----------|------|-------------|
| `target_session_key` | str | Target child Agent's session key |
| `message` | str | Message content |
| `max_turns` | int | Maximum rounds (default 1) |

Delivers a targeted message via `MessageBus.publish_inbound()` with `metadata.injected_event = "subagent_message"`.

#### agents_list — Available Agent List

Returns the `allow_agents` whitelist from configuration.

#### subagents_list — Child Agent Status List

Returns active and recent child Agents under the current session:

```
Subagents: total=5, active=3, recent=2

Active:
  - [abc12345] research (depth=1, role=leaf, model=gpt-4, runtime=30s, pending=0)
  - [def67890] analysis (depth=1, role=leaf, model=gpt-4, runtime=2.5m, pending=0)
  - [ghi11223] writer (depth=1, role=orchestrator, model=gpt-4, runtime=1.2h, pending=2)

Recent:
  - [jkl44556] lookup status=ok runtime=45s
  - [mno77889] verify status=timeout runtime=5.0m
```

#### sessions_kill — Cancel Child Agent

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | str | required | Run ID to kill |
| `reason` | str | "killed" | Kill reason |

Cancels a running child Agent. Only the controller session can kill. Supports cascade kill (recursive kill all children). Kill reconciliation arbitrates with any concurrent completion.

`kill_all_controlled_subagent_runs(requester_session_key)` — Kill all killable children of a session in one call.

#### sessions_steer — Steer/Restart Child Agent

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_id` | str | required | Run ID to steer |
| `new_instructions` | str | required | New instructions to inject |

Injects new instructions into a running child Agent. The run transitions to INTERRUPTED state with `pause_reason="steer"` and increments `generation`.

### 10. Hook Protocol

The Hook mechanism allows external code to listen for child Agent lifecycle events:

```python
from future_subagent.hooks.base import register_start_hook, register_stop_hook
from future_subagent.hooks.progress import register_spawned_hook, register_ended_hook, register_delivery_target_hook

async def on_start(event: SubagentStartEvent):
    print(f"Subagent started: {event.child_session_key}")

async def on_stop(event: SubagentStopEvent):
    print(f"Subagent stopped: {event.child_status}")

async def on_delivery_target(run, target_session_key):
    return None  # return a session_key to redirect, or None

register_start_hook(on_start)
register_stop_hook(on_stop)
register_delivery_target_hook(on_delivery_target)
```

| Event | Fields |
|-------|--------|
| `SubagentStartEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_goal` |
| `SubagentStopEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_status`, `child_summary`, `duration_ms` |

Hooks execute sequentially in registration order; exceptions are swallowed and do not interrupt the flow.

### 11. Coexistence with the Existing System

| Dimension | Existing subagent (`agent/tools/subagent/`) | New subagent (`future_subagent/`) |
|-----------|---------------------------------------------|---------------------------|
| Tool names | `subagent` | `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list` |
| Manager | `SubagentManager` (singleton) | `SubagentRegistry` (dict + SQLite) |
| Child Agent | Commander + Worker (two layers) | Direct spawn of LangGraph agent |
| Depth | Single level | Multi-level nesting (default 3 levels) |
| Communication | One-way return | Bidirectional (sessions_send) |
| Knowledge graph | Yes (draft→distill→ingest) | Not yet |
| Delivery channel | MessageBus | MessageBus (shared) |
| Middlewares | — | Summarization + IterationBudget + ToolGuardrails + ToolCallNormalize + HeartbeatStaleness |

Both tool sets are registered in `_MAIN_TOOLS_BUILDERS` simultaneously without conflict, enabling gradual migration.

### 12. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Child Agent execution | `CompiledStateGraph.ainvoke()` | Reuses LangGraph infrastructure, native async |
| Delivery channel | Reuse `MessageBus.publish_inbound()` | Existing mechanism, no rebuild needed |
| Persistence | aiosqlite only (no JSON fallback) | Already a project dependency; SQLite is reliable cross-platform |
| Sandbox | No ACP port | Same-process execution; permissions controlled via tool deny lists |
| Yield implementation | `asyncio.Event` + Registry callback | Python has no gateway steering; Event is equivalent |
| A2A communication | MessageBus + session key routing | Reuses existing messaging mechanism |
| Coexistence strategy | Independent new module, separate tool namespace | Gradual migration without breaking existing functionality |
| Full fork context | `agent.aget_state()` from checkpointer | Decision 9: no external `parent_messages` param needed |
| Blocked tools | `sessions_spawn`, `sessions_yield`, `skill_manage`, `memory` | Prevents recursive spawn and privilege escalation |

---

## Configuration

All configuration is managed via `SubagentConfig` (Pydantic model, singleton):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_spawn_depth` | 3 | Maximum nesting depth |
| `max_children_per_agent` | 5 | Max concurrent children per agent |
| `run_timeout_seconds` | 300.0 | Child agent execution timeout |
| `require_agent_id` | False | Whether agent_id is mandatory |
| `allow_agents` | `["*"]` | Allowed agent_id whitelist |
| `default_cleanup` | "delete" | Default cleanup policy |
| `default_context_mode` | ISOLATED | Default context mode |
| `announce_retry_max` | 3 | Max delivery retries |
| `announce_retry_delay_base_ms` | 1000 | Exponential backoff base (1s, 2s, 4s) |
| `delivery_suspend_soft_cap` | 25 | Soft delivery suspension threshold |
| `delivery_suspend_hard_cap` | 50 | Hard delivery suspension threshold |
| `delivery_suspend_target` | 10 | Target count for pressure pruning |
| `lifecycle_grace_period_seconds` | 15.0 | Grace period before error/timeout finalization |
| `sweeper_interval_seconds` | 60 | Sweeper scan interval |
| `orphan_recovery_delay_seconds` | 120 | Orphan recovery delay |
| `announce_expiry_ms` | 7,200,000 | Delivery soft expiry (2h) |
| `announce_hard_expiry_ms` | 86,400,000 | Delivery hard expiry (24h) |
| `max_announce_retry_count` | 10 | Max announce retry count |
| `stale_unended_threshold_seconds` | 7200 | Stale unended run threshold |
| `recent_ended_window_seconds` | 1800 | Recent ended window for display |
| `steer_rate_limit_ms` | 2000 | Steer rate limit |
| `archive_after_minutes` | 1440 | Auto-archive after minutes |
| `attachments_enabled` | True | Whether attachments are allowed |
| `attachments_max_files` | 50 | Max files per spawn |
| `attachments_max_file_bytes` | 1MB | Max single file size |
| `attachments_max_total_bytes` | 5MB | Max total attachment size |

---

## Project Status

**All 7 phases completed (2026-07-15). Robustness-plan-v3 enhancements completed (2026-07-22). Bug fixes + OpenClaw alignment + depth alignment + wiring fixes completed (2026-07-23).** 203 tests pass. See [AGENTS.md](./AGENTS.md) for conventions and [decisions.md](./docs/decisions.md) for technical decisions.
