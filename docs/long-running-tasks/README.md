# ⏳ Long-Running Tasks: TaskFlow, Budgets, Deadlines, Memory & Continuity

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent runs work that outlives a single turn: a durable SQLite DAG engine (`taskflow_*`, 12 tools) tracks dependent steps across conversation turns, dispatches each step to a detached child subagent, aggregates token/cost spend against a budget, expires overdue or idle flows from a background sweeper, and carries context forward through a three-layer memory system, a pre-compression memory flush, a summary↔TaskFlow bridge, one-line tool-output summaries, cross-session continuity, and automatic re-injection of active flows into the system prompt.

Source of truth: `agent/tools/taskflow/**`, `agent/tools/memory.py`, `agent/tools/memory_tiered.py`, `agent/middlewares/memory_flush.py`, `agent/middlewares/summarization.py` (LT-7 block), `agent/middlewares/task_intent.py`, `agent/middlewares/todo_continuation.py`, `context_engine/session_continuity.py`, `workspace/prompt_builder.py`, `pub/func/message/tool_output_prune.py`, `agent/tools/subagent/registry/sweeper.py`, `config/features/**`. Every constant, signature and line number below was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [TaskFlow DAG Engine](#-taskflow-dag-engine)
- [Token / Cost Budget](#-token--cost-budget)
- [Task Deadline](#-task-deadline)
- [Progress Report](#-progress-report)
- [Idle Detection](#-idle-detection)
- [Tiered Memory](#-tiered-memory)
- [Pre-Compression Memory Flush](#-pre-compression-memory-flush)
- [Summary ↔ TaskFlow Coordination](#-summary--taskflow-coordination)
- [Tool Output Summarization](#-tool-output-summarization)
- [Session Continuity](#-session-continuity)
- [TaskFlow Auto-Resume](#-taskflow-auto-resume)
- [Configuration Registry](#-configuration-registry)
- [Architecture Diagram](#-architecture-diagram)
- [API Reference](#-api-reference)
- [Testing](#-testing)
- [Known Limitations](#-known-limitations)

## 🎯 Overview

The long-running-task stack lets the main agent decompose a multi-turn job into a **durable flow** whose steps may depend on each other, dispatch each ready step to a child subagent, and survive process restarts. Seven subsystems cooperate:

| # | Subsystem | Entry point | Durable where |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG engine** | `agent/tools/taskflow/` | `data/taskflow_registry.db` (SQLite, WAL) |
| 2 | **Token / cost budget** | `taskflow_budget`, `taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **Deadline** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **Idle detection** | sweeper `_scan_stale_waiting_taskflows` | `wait_json` stale markers |
| 5 | **Tiered memory** | `memory` tool actions + `agent/tools/memory_tiered.py` | `workspace/memory/*.md` + `facts/*.md` |
| 6 | **Pre-compression flush** | `agent/middlewares/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 7 | **Continuity / auto-resume** | `context_engine/session_continuity.py`, `workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + prompt blocks |

The design contract throughout is **error-as-text**: tools never raise business errors at the model; they return human-readable strings beginning with `Error:`. Every background hook is **fail-open** — an unavailable registry or crashed sweeper degrades to "no long-running-task context", never to a broken turn.

## 🧩 TaskFlow DAG Engine

### Status enums (`agent/tools/taskflow/config.py`)

```python
TABLE_NAME = "task_flows"          # config.py:5
INITIAL_REVISION = 1               # config.py:8

class TaskFlowStatus(StrEnum):     # config.py:11
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

class StepStatus(StrEnum):         # config.py:21
    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})   # config.py:35
```

### Table schema (`agent/tools/taskflow/registry/store_sqlite.py`)

```sql
CREATE TABLE IF NOT EXISTS task_flows (
    flow_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    wait_json TEXT,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    child_session_key TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    token_budget INTEGER DEFAULT 0,
    deadline_ts REAL
);
```

The DAG itself (`steps[]`, `results[]`, `depends_on`, `creator_session_key`) lives entirely inside `state_json` — no schema migration is needed to add DAG fields. The token/cost/deadline columns were added by additive migrations (`_TOKEN_COLUMN_DDL`, `_DEADLINE_COLUMN_DDL`, `store_sqlite.py:83-129`). The WAL pragma is applied once per process and `PRAGMA busy_timeout = 5000` precedes every statement (`store_sqlite.py:234-271`).

### Step status machine

```
blocked ──(deps satisfied)──▶ ready ──(dispatch)──▶ dispatched ──(resume)──▶ done
```

| Status | Meaning | Set by |
| :--- | :--- | :--- |
| `blocked` | at least one `depends_on` id is not `done`; no child spawned | `taskflow_run_task` |
| `ready` | all dependencies satisfied, awaiting dispatch | registration, or `unlock_dependents` after a resume |
| `dispatched` | a detached child session was spawned; `child_session_key` + `dispatched_at` recorded | `taskflow_run_task` / `taskflow_dispatch` |
| `done` | a result was injected by `taskflow_resume` | `taskflow_resume` |

`done` means **"a result was injected"**, not "the child succeeded" (see [Known Limitations](#-known-limitations)). Legacy steps without a `status` field are derived: a step carrying `child_session_key` is treated as `dispatched`, otherwise `ready` (`_shared.py:85`, `step_status`).

### `depends_on` semantics

`deps_satisfied(step, steps)` (`_shared.py:99`) is true iff every `depends_on` id exists in the current step list **and** is `done`. Missing/empty `depends_on` is trivially satisfied. An unknown dependency id is never satisfied, and a **self-dependency is never satisfied** — so a self-referential step stays safely blocked instead of entering an unlock loop. `unlock_dependents(steps)` (`_shared.py:137`) is a **single pass** over the list, which structurally prevents a dependency cycle from looping. `taskflow_run_task` rejects unknown dependency ids *before* any spawn or state change.

### Tool family (12 tools)

All tools are `async`, decorated `@tool("taskflow_…")`, tagged `metadata={"scope": "main_only"}` and `handle_tool_error=True` by `build_taskflow_tools()` (`tools/__init__.py:43-55`). Only the main agent may manage shared flow state; the subagent tool policy drops the family unconditionally.

| Tool | Purpose |
| :--- | :--- |
| `taskflow_create` | Create a flow at revision 1; optionally set `deadline_hours` |
| `taskflow_run_task` | Register a step and dispatch it (or record it `blocked`) |
| `taskflow_dispatch` | Batch-dispatch several ready steps all-or-nothing |
| `taskflow_wait_all` | Flow-scoped bounded poll for dispatched steps to settle |
| `taskflow_resume` | Idempotently inject a child result, unlock dependents, aggregate tokens |
| `taskflow_set_waiting` | Park the flow in `waiting` with a reason |
| `taskflow_summary` | Read-only re-read (also the post-conflict re-read) |
| `taskflow_progress` | Human-readable progress/completion report |
| `taskflow_budget` | Query or set the token/cost budget |
| `taskflow_finish` / `taskflow_fail` / `taskflow_cancel` | Terminal transitions |

```python
# agent/tools/taskflow/tools/taskflow_run_task.py:38
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

### Dispatch — `taskflow_dispatch` batch semantics

`taskflow_dispatch(flow_id, step_ids, expected_revision=None, session_id="")` (`taskflow_dispatch.py:36`) validates **every** id before anything spawns. An id is dispatchable when its status is `ready`, or `blocked` with dependencies already satisfied. An unknown id, a duplicate, or a step already `dispatched`/`done` rejects the whole call with zero spawns. On success the steps are spawned sequentially through the shared `_dispatch.dispatch_child` seam (`_dispatch.py:10`) and persisted in **one** `update_flow` call. If a spawn fails mid-batch the loop stops, already-spawned children are persisted so none is silently dropped, and the error names both the failed and dispatched step ids. The flow-level `child_session_key` is deliberately left untouched — per-step child keys are authoritative.

### Wait — `taskflow_wait_all` flow scope

`taskflow_wait_all(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id="")` (`taskflow_wait_all.py:110`) polls **only** the child sessions recorded on *this* flow's `dispatched` steps, so unrelated background children can never block the return. An unknown or already-cleaned run is treated as settled (it never hangs). The interval is clamped to `TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]` (0.05 s). On timeout it returns a partial report plus the instruction to `taskflow_resume` settled children and call `wait_all` again. It deliberately does **not** reuse `sessions_yield`, which fires only when all of a requester session's children settle.

### Optimistic locking

Every mutation goes through `UPDATE … WHERE flow_id = ? AND expected_revision = ?` and bumps the revision by exactly 1 (`store_sqlite.py:460-481`). Zero matched rows means a conflict:

```python
# FlowConflictError message (store_sqlite.py:173)
"TaskFlow '<id>' revision conflict: expected_revision=2 but latest revision=3;
 re-read with taskflow_summary and retry with expected_revision=3"
```

When a child has **already spawned** but the write loses the race, `update_flow_with_conflict_retry()` (`_shared.py:191`) re-reads the fresh flow, rebuilds the state via a `build_state` callback, and retries up to `PERSIST_MAX_ATTEMPTS = 3`. If the flow vanished or turned terminal, or retries are exhausted, it returns an error naming every spawned `child_session_key` and instructs the caller to recover them **by key, never re-dispatch**.

## 🪙 Token / Cost Budget

`taskflow_budget` (`taskflow_budget.py:10`) is both the query and the setter:

```python
@tool("taskflow_budget")
async def taskflow_budget(
    flow_id: str,
    action: str = "query",           # "query" | "set"
    token_budget: int | None = None,
    expected_revision: int | None = None,
) -> str
```

- **`query`** reports `total_tokens`, `total_cost`, the budget, tokens remaining, and a status: `EXCEEDED` when `total_tokens >= budget`, `WARNING` when usage ≥ `MODEL_PRICING["budget_warn_threshold"]` (0.80), else `ok`. With no budget set the percentage is reported as not set.
- **`set`** requires a positive integer `token_budget`, rejects terminal flows, and writes through the optimistic lock (pass `expected_revision` to fail fast).

Spend is aggregated in `taskflow_resume` when the caller passes `token_usage` (`taskflow_resume.py:108-122`):

```python
token_usage={"input_tokens": 1200, "output_tokens": 800, "model_name": "deepseek-chat"}
```

The tool lazily imports `from config.features import MODEL_PRICING`, looks up the model in `MODEL_PRICING["model_pricing_per_m_tokens"]` (falling back to `_default`), and computes:

```
total_tokens = existing_total_tokens + input_tokens + output_tokens
cost_delta   = input_tokens  * pricing["input"]  / 1_000_000
             + output_tokens * pricing["output"] / 1_000_000
total_cost   = round(existing_total_cost + cost_delta, 6)
```

`MODEL_PRICING` lives in `config/features/infra_side/model_pricing.py`:

| Model | input (USD / 1M) | output (USD / 1M) |
| :--- | :--- | :--- |
| `glm-5` | 0.5 | 1.5 |
| `deepseek-chat` | 0.14 | 0.28 |
| `kimi-latest` | 0.55 | 2.19 |
| `_default` | 1.0 | 3.0 |

`budget_warn_threshold` = `0.80`.

## ⏰ Task Deadline

`taskflow_create` accepts an optional `deadline_hours` (`taskflow_create.py:17`):

```python
@tool("taskflow_create")
async def taskflow_create(
    flow_id: str,
    description: str = "",
    initial_state: dict | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
    deadline_hours: float | None = None,
) -> str
```

When `deadline_hours` is positive the tool stores `deadline_ts = time.time() + deadline_hours * 3600` in the `deadline_ts` column. `taskflow_summary` renders the deadline and marks it `EXCEEDED` once passed. The actual enforcement is the background **sweeper** (`agent/tools/subagent/registry/sweeper.py`), which runs `_expire_overdue_taskflows()` on every sweep cycle (`sweeper.py:123`):

```python
overdue = await taskflow_store.get_overdue_flows(time.time())
for flow in overdue:
    state["failure_reason"] = "Deadline exceeded: " + time.strftime("%Y-%m-%d %H:%M", ...)
    update_flow(flow_id, flow["expected_revision"], state=state, status=TaskFlowStatus.FAILED.value)
```

Overdue flows are marked `failed`; already-terminal flows are excluded by the query, and per-flow exceptions are logged and swallowed so one bad row cannot abort the sweep.

## 📊 Progress Report

`taskflow_progress` (`taskflow_progress.py:22`) is a read-only report with completion percentage, breakdown, next steps and an estimated remaining time:

```python
@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str
```

Output shape:

```
Progress Report: <flow_id>
  Status: running
  Description: <first 80 chars>
  Completion: 3/5 steps (60%)
  Breakdown: done=3 · dispatched=1 · ready=0 · blocked=1
  Next steps:
    → [step-4] <task, first 60 chars>
    ⊘ [step-5] <task, first 60 chars>
  Est. remaining: ~12.5 minutes (based on 3 completed steps)
  Waiting on: <wait reason>            # only when the flow is waiting
  Results injected: 3                  # only when results exist
```

Status icons are `done=✓`, `dispatched=→`, `ready=○`, `blocked=⊘` (`taskflow_progress.py:14`). The estimate is produced only when at least **two** `done` steps carry a `dispatched_at` timestamp; it averages the spacing between dispatch timestamps and multiplies by the remaining step count. A flow with no steps returns `Progress: flow_id=…, status=…` plus `No steps registered yet.`

## 💤 Idle Detection

A flow parked with `taskflow_set_waiting` may have its child crash without ever resuming. The sweeper detects this with `_scan_stale_waiting_taskflows()` (`sweeper.py:154`):

1. Read all `waiting` flows.
2. `timeout_secs = TASKFLOW_INFRA["waiting_timeout_hours"] * 3600` (24 h by default).
3. For a wait payload with `set_at`, skip while `now - set_at <= timeout_secs`.
4. Check child liveness via `get_run_by_child_session_key(flow["child_session_key"])` + `is_live_unended_run(run)`. If the child is still live, skip; if the liveness import itself fails, the child is treated as dead.
5. Otherwise stamp a **non-destructive marker** into `wait_json`: `stale_detected_at` and `stale_child_session_key`.

The flow is **never auto-failed** by idle detection — the marker is advisory and refreshed each cycle. Independently, `taskflow_summary` renders the wait status and prints `wait_status: STALE (waiting X.Xh, timeout=24h) — child session may have crashed; consider taskflow_resume with a failure result or re-dispatch` when the wait exceeds `TASKFLOW_INFRA["waiting_timeout_hours"]` (`taskflow_summary.py:67-90`).

## 🧠 Tiered Memory

Three layers, distinguished by *how* they reach the model:

| Layer | Store | Location | In the prompt? |
| :--- | :--- | :--- | :--- |
| **L1 — curated memory** | `MEMORY.md` (agent notes) + `USER.md` (user profile) | `workspace/memory/` (`MEMORY_DIR`) | Yes — a frozen snapshot, always injected |
| **L2 — structured facts** | `facts/{category}.md`, 5 fixed categories | `workspace/memory/facts/` (`FACTS_DIR`) | Only a one-line index; full content is read on demand |
| **L3 — raw history** | `mes_memory.db` (SQLite, WAL, FTS5) | `src/store/mes_memory/mes_memory.db` | No — retrieved by `context_engine` / `message_search` |

Files are **plain text entries separated by the delimiter `§` on its own line** — `ENTRY_DELIMITER = "\n§\n"` (`agent/tools/memory.py:53`), no YAML frontmatter and no bullet prefixing. Entries may be multiline.

Layer 1 is managed by `MemoryStore` (`memory.py:104`): per-file character limits `2200` (memory) and `1375` (user), an injection scan (`_MEMORY_THREAT_PATTERNS`, `memory.py:68`) that rejects prompt-injection and credential-exfiltration content, cross-platform file locking, atomic writes, and exact-match dedup. The live entries are mutated immediately, while the prompt uses a **frozen snapshot** captured at `load_from_disk()` to keep the prefix cache stable for the session.

Layer 2 is managed by `TieredMemoryStore` (`agent/tools/memory_tiered.py:19`) with categories `environment`, `project`, `decisions`, `user_prefs`, `tool_lessons` (`TIERED_MEMORY["facts_categories"]`) and a per-file cap of `TIERED_MEMORY["facts_char_limit"]` = 4000 chars. When a file overflows, the **oldest** entry is evicted first; exact duplicates are reported as already present.

The fact operations are **actions of the single `memory` tool**, not separate tools (`memory.py:641`):

```python
# MemoryActionSchema.action
Literal["add", "replace", "remove", "fact_add", "fact_read", "fact_search"]
```

| Action | Required args | Returns |
| :--- | :--- | :--- |
| `fact_add` | `content`, `target` (category) | JSON `{"success", "message", "category", "entry_count", "usage"}` |
| `fact_read` | `target` = category or `"all"` | JSON `{"success": true, "facts": {category: text}}` |
| `fact_search` | `content` (substring query) | JSON `{"success": true, "results": [{"category", "fact"}], "count"}` |

`prompt_builder.build_system_prompt` injects the L1 snapshots via `format_for_system_prompt` and appends the L2 listing `FACTS (on-demand, use memory tool with fact_read/fact_search): …` (`workspace/prompt_builder.py:271-282`). The `memory` tool is tagged `scope="main_only"`, so subagents never see it.

## 🔥 Pre-Compression Memory Flush

Before the summarization middleware discards old messages, `agent/middlewares/memory_flush.py` gives a cheap model one last chance to persist durable facts into `MEMORY.md`. The trigger is `should_flush(discarded_messages, estimated_tokens)` (`memory_flush.py:43`):

```python
if not MEMORY_FLUSH["enabled"]:
    return False
total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
if total_chars >= MEMORY_FLUSH["force_flush_chars"]:   # 50_000
    return True
return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]   # 8_000
```

When it fires, `run_memory_flush` (async) / `run_memory_flush_sync` builds the model with an injected factory and one plain-text extraction prompt (`_FLUSH_PROMPT`, `memory_flush.py:19`) whose output is a `§`-separated list of `Environment / Project / Decision / User / Tool` facts. An empty result or the literal `(none)` is skipped. The extracted text is handed to `MemoryStore.append_entries(new_entries)` (`memory.py:281`), which splits on `§`, scans every candidate for injection, dedups against the existing set, appends, evicts oldest entries while over 2200 chars, and performs one atomic write. `append_entries` always targets `MEMORY.md`. Every failure path returns `False` and is swallowed — the flush can never block compression.

⚠️ **Wiring status.** `Summarization.__init__` accepts `memory_store` / `llm_factory` (both default `None`, `summarization.py:623-624`) and calls the flush only when both are set, inside `_apply_compression` (`summarization.py:1703`) and `_aapply_compression` (`summarization.py:1791`). The current production instantiations — main agent `agent/core.py:170` and subagent `agent/tools/subagent/spawn/core.py:784` — do **not** pass them, so the flush is implemented and tested but latent until a call site supplies the store and a factory shaped `factory(model=…, max_tokens=…, timeout=…)`.

## 🔗 Summary ↔ TaskFlow Coordination

When compression builds its LLM prompt, `_get_taskflow_context_sync(session_id)` (`agent/middlewares/summarization.py:262`) renders this session's active flows and appends them as the **last** part of the summary prompt (`_build_summary_prompt`, `summarization.py:1431-1434`):

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

The block is headed `## Current TaskFlow State (authoritative)` (`summarization.py:286`) and, for up to three flows owned by the session (matched through `requester_session_key(session_id)`), lists the flow id/status, description, `done/total` progress with the status breakdown, the last two completed steps, the first two pending steps, and any wait reason. It reuses the DAG helpers `step_status` and `steps_summary`, and is fully fail-open (`except Exception → ""`). The deterministic fallback summary (`_build_static_fallback_summary`) does **not** include this block; it is an LLM-prompt-only addition.

## ✂️ Tool Output Summarization

Older oversized `ToolMessage` content is normally cleared to a marker during non-LLM pruning. `pub/func/message/tool_output_prune.py` replaces the bare `_PRUNE_MARKER = "[Old tool result content cleared]"` (`tool_output_prune.py:22`) with a **one-line, tool-specific summary** so the model retains a hint of what was in the result:

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_summary"   -> "[taskflow_summary] {len} chars"
"taskflow_run_task"  -> "[taskflow_run_task] dispatched, {len} chars"
"taskflow_resume"    -> "[taskflow_resume] injected result, {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)` (`tool_output_prune.py:103`) walks messages newest→oldest, stops at the first summary message, protects the newest `prune_protect_tokens` (40 000), skips protected tools (`{"memory", "skill_view", "skill_list"}`), and only commits when the freed tokens reach `prune_min_reduction_tokens` (5 000). Replaced messages are `model_copy` clones carrying `additional_kwargs["status"] = "compacted"` and `["original_length"]`. Summaries are capped at 200 chars; any template exception falls back to the marker. It is called from `Summarization._run_non_llm_strategies` (`summarization.py:1538`).

## 🔄 Session Continuity

When a session is cleared, `context_engine/session_continuity.py` persists an end-state so the next session can offer continuity. `server/DAO/messages.py::clear_session` calls `auto_save_on_session_end(session_id)` as **step 0**, before any deletion (`server/DAO/messages.py:27-33`). That function:

1. Resolves `channel_id`/`chat_id` through `runtime.relation_register` (`_get_channel_chat_for_session`, `session_continuity.py:167`).
2. Reads the last 3 turns and clips the last AI reply to `_MAX_SUMMARY_CHARS = 500` (`session_continuity.py:28`).
3. Collects active flow ids for the session.
4. Writes `save_session_end_state(...)` to a JSON file at `src/data/session_continuity/{safe-key}.json` (`session_continuity.py:25`), with fields `last_session_id`, `ended_at`, `ended_ts`, `summary`, `taskflow_ids`.

The next session reads it via `build_continuity_prompt(session_id)` (`session_continuity.py:80`), called by `_build_continuity_block` in `workspace/prompt_builder.py:169` and injected when the full prompt is built (`prompt_builder.py:289-295`):

```
## Last Session (continuity)
Last conversation ended with: <summary ≤ 500 chars>
Related tasks: <up to 3 flow ids>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

A session never receives its own state (`last_session_id == session_id → ""`). Because the lookup requires **both** a channel id and a chat id, pure WebSocket sessions with no channel binding get no continuity block. Storage is filesystem JSON (keyed by `channel:chat`, falling back to `session_id`), not a database.

## ♻️ TaskFlow Auto-Resume

Active flows are re-surfaced into the system prompt so a fresh session can pick up unfinished work. Three independent readers use the same recipe — `requester_session_key(session_id)` + `get_active_flows_sync()` + a `state["creator_session_key"]` filter:

| Reader | Location | Purpose |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | `## Pending TaskFlows` in the system prompt |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization.py:262` | TaskFlow block in the compression summary prompt (LT-7) |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:186` | `taskflow_ids` in the persisted continuity state |

`creator_session_key` is stamped on the flow at creation (`taskflow_create.py:38`) as `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"` (`_shared.py:21`). `get_active_flows_sync()` (`store_sqlite.py:538`) returns only `running` and `waiting` flows ordered by revision, using the stdlib `sqlite3` path that works without an event loop; failures return `[]`.

The system-prompt block (`prompt_builder.py:140`) looks like:

```
## Pending TaskFlows
- [running] flow-1: "<description>" | 2/5 steps done | next: step-3 "<task>"
Use taskflow_summary to inspect a flow and continue execution.
```

It is capped at three flows and suppressed when the prompt is built with a file filter (`selected_file_names is not None`). Every read is fail-open.

## ⚙️ Configuration Registry

All tunables live under `config/features/` as a hand-rolled **per-object `TypedDict` registry**. Each module defines `class XxxConfig(TypedDict)` plus a module-level constant `XXX: XxxConfig = {…}`. Env-aware modules define a builder `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig` that reads `env or os.environ` and materialises the constant at import time. The single env helper is `_env_int(name, default, env)` (`config/features/_env.py:9`), which accepts `1/true/yes/on` and `0/false/no/off/""` and never raises.

The registry currently holds **38 feature objects** — 20 on the agent side (`config/features/agent_side/`) and 18 on the infra side (`config/features/infra_side/`) — re-exported through each package `__init__.py` and aggregated by `config/features/__init__.py`. Consuming code imports the constant and indexes it directly (for example `ITERATION_BUDGET["default_max_iterations"]`); there is no `get_feature`/`load_feature` accessor. `config/__init__.py:38-39` derives `API_HOST`/`API_PORT` from `GATEWAY`.

The constants most relevant to this document:

| Config object | Field | Value |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA` (`agent_side/taskflow_infra.py`) | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING` (`infra_side/model_pricing.py`) | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `TIERED_MEMORY` (`agent_side/tiered_memory.py`) | `facts_char_limit` | 4000 |
| | `facts_index_max_chars` | 200 (declared; not consumed by live code) |
| | `facts_categories` | environment, project, decisions, user_prefs, tool_lessons |
| `MEMORY_FLUSH` (`agent_side/memory_flush.py`) | `enabled` | `MEMORY_FLUSH_ENABLED` (default 1) |
| | `model` | `MEMORY_FLUSH_MODEL` (default "") |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION` (`agent_side/summarization.py`) | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY` (`infra_side/mes_memory.py`) | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ Architecture Diagram

```
                          ┌──────────────────────────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (12, main_only)   │          │  add/fact_add/…      │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │ TieredMemoryStore(L2)│                 │ ─ FACTS index (L2)     │
│ state_json DAG    │          │ mes_memory.db (L3)   │                 │ ─ Pending TaskFlows    │
└────────┬──────────┘          └──────────────────────┘                 │ ─ Last Session         │
         │                                                              └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
                                                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+LT-7 TaskFlow)  │   │
└──────────────────────────────┘                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

## 📚 API Reference

### TaskFlow tools

| Tool | Signature | Returns |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | created id/status/revision (+ deadline) |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, session_id)` | dispatched step, or `blocked` with pending deps |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | dispatched step ids + revision |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | per-step settled report (complete or partial) |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None)` | resumed status, unlocked steps, step-status counts |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None)` | waiting status + revision |
| `taskflow_summary` | `(flow_id)` | full flow state incl. wait/deadline status |
| `taskflow_progress` | `(flow_id)` | completion %, breakdown, next steps, est. remaining |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None)` | budget report, or set confirmation |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None)` | terminal `done` |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None)` | terminal `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None)` | terminal `cancelled` |

### Memory tool actions

| Action | Signature | Returns |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON success/error |
| `fact_add` | `memory(action="fact_add", target=<category>, content=<fact>)` | JSON `{success, message, category, entry_count, usage}` |
| `fact_read` | `memory(action="fact_read", target=<category>\|"all")` | JSON `{success, facts: {category: text}}` |
| `fact_search` | `memory(action="fact_search", content=<query>)` | JSON `{success, results: [{category, fact}], count}` |

### Key functions & constants

| Symbol | Location | Role |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | Lifecycle / DAG enums |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py:402` | Optimistic-locked mutation |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py:538` | Cross-session active-flow read |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py:489,505` | Sweeper queries |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:99,137` | DAG transitions |
| `update_flow_with_conflict_retry` | `_shared.py:191` | Never-lose-a-spawned-child persist |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | Deadline enforcement |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | Idle detection marker |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization.py:262` | LT-7 summary coordination |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | Auto-resume prompt block |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:103` | One-line tool summaries |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | Continuity save hook |
| `should_flush` / `run_memory_flush` | `agent/middlewares/memory_flush.py:43,65` | Pre-compression flush |
| `append_entries` | `agent/tools/memory.py:281` | Batch MEMORY.md append |

## 🧪 Testing

The TaskFlow suite lives under `tests/agent/tools/taskflow/` (fourteen `unit` test files plus a shared `conftest.py`):

| Test file | Covers |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD, revision bumping, optimistic-concurrency conflict, WAL, sync accessors, active/waiting/terminal filters |
| `test_step_graph.py` | `deps_satisfied`, `mark_step_done`, `unlock_dependents`, legacy status derivation, self-dependency guard |
| `test_summary_dag.py` | `taskflow_summary` DAG rendering |
| `test_resume_dag.py` | Resume marks done, unlocks dependents, partial completion, idempotent no-op |
| `test_taskflow_tools.py` | Full create→run→resume→finish across restart, conflict, terminal transitions |
| `test_taskflow_dispatch.py` | Batch dispatch, all-or-nothing validation, mid-batch failure persistence |
| `test_dag_e2e.py` | Full parallel DAG flow across a process restart |
| `test_conflict_persistence.py` | Spawned-children persistence after conflict, retry exhaustion |
| `test_taskflow_wait_all.py` | Flow-scoped wait, timeout partial report, unrelated live child |
| `test_run_task_dag.py` | Blocked registration, satisfied dispatch, unknown-dependency error |
| `test_taskflow_progress.py` | Completion/breakdown/next-steps/est-remaining/waiting |
| `test_token_budget.py` | Token aggregation, cost math, budget query/set/warning/exceeded |
| `test_deadline.py` | `deadline_hours`, summary rendering, sweeper expiry |
| `test_idle_detection.py` | Active/stale wait status, sweeper marker, live-child skip |

Cross-cutting suites: `tests/agent/middlewares/test_memory_flush.py` (flush thresholds and `append_entries`), `tests/agent/tools/test_memory_tiered.py` (tiered facts), `tests/context_engine/test_session_continuity.py` (continuity save/prompt), `tests/agent/middlewares/test_todo_continuation.py` (turn-end continuation), `tests/pub/func/message/test_tool_output_prune.py` (one-line summaries), and `tests/workspace/test_prompt_builder_taskflow.py` (pending-flow prompt injection).

Run just this area with the standard uv/pytest tooling:

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py tests/agent/tools/test_memory_tiered.py -q
```

For the full process-isolated suite use `uv run python tests/run_tests_split.py` (Group A runs the `unit` files, Group B runs the `module`/`integration` files).

## ⚠️ Known Limitations

- **`done` is not success.** A step's `done` means "a result was injected"; there are no `failed`/`skipped` step statuses. `taskflow_resume` marks the step `done` and unlocks successors even when the child reported an error. Failure-aware step transitions are intentionally deferred.
- **`taskflow_wait_all` is flow-scoped by design.** It waits only on children recorded on the given flow's dispatched steps, and unknown/already-cleaned runs count as settled. There is no global "wait for every active flow" primitive.
- **Idle detection is advisory.** The sweeper stamps `stale_detected_at` / `stale_child_session_key` into `wait_json` but never auto-fails a stale `waiting` flow; a human or the model must act on the marker.
- **The pre-compression memory flush is latent.** Production instantiations of `Summarization` (main agent and subagent) do not pass `memory_store` / `llm_factory`, so the flush does not run until a call site wires them; the code is implemented and tested but currently inert.
- **Continuity is channel-bound.** `build_continuity_prompt` requires both a channel id and a chat id, so sessions without a channel binding receive no continuity block. Storage is per-key JSON on disk, not a database.
- **Three duplicate active-flow scans.** `prompt_builder._build_taskflow_block`, `summarization._get_taskflow_context_sync`, and `session_continuity._get_active_taskflow_ids_sync` implement the same query independently; they must be kept in sync.
- **Registry size is 38, not 35.** The config registry holds 38 feature objects (20 agent-side + 18 infra-side); the infra-side contract test under-counts at 17 because it omits `MODEL_PRICING`.
- **`facts_index_max_chars` is declared but unused.** The `TIERED_MEMORY` field exists; no live code reads it.
- **Package re-export gap.** `agent/tools/taskflow/__init__.py` re-exports only eleven names; `taskflow_dispatch` and `taskflow_wait_all` are reachable through `build_taskflow_tools()` but omitted from the package `__all__`.
- **The LT-7 TaskFlow block is LLM-prompt only.** The deterministic fallback summary used on LLM failure does not include `## Current TaskFlow State`.
- **Token accounting is caller-supplied.** Cost is computed only when `taskflow_resume` receives a `token_usage` dict; steps whose results are injected without it contribute zero tokens and zero cost.
