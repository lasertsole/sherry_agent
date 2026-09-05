# Heartbeat — Periodic Task Check Service

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Heartbeat** is EMA AI Agent's periodic wake-up service: every tick it reads [`workspace/HEARTBEAT.md`](../../../../workspace/HEARTBEAT.md), lets the auxiliary LLM decide whether there are active tasks, and — when there are — executes them through a dedicated agent run and delivers the result through a notification gate.

---

## Motivation

After a conversation ends, the Agent may sit idle while external work remains:
- Tasks awaiting execution (written to HEARTBEAT.md by the agent or the user)
- Monitoring tasks that need periodic checks
- Long-running work that needs continued progress

Heartbeat provides a **lightweight polling mechanism** that enables the Agent to work proactively during idle periods.

---

## Architecture

```
┌──────────────────────────────────────────┐
│            HeartbeatService              │
├──────────────────────────────────────────┤
│  asyncio loop (sleep backoff → tick)     │
│  ├─ Phase 1: Read HEARTBEAT.md           │
│  ├─ Phase 2: LLM decision (skip/run)     │
│  └─ Phase 3: Execute + notification gate │
└──────────────────────────────────────────┘
```

### Module Responsibilities

| File | Responsibility |
|------|----------------|
| [`scripts/base.py`](scripts/base.py) | `HeartbeatService` class: asyncio loop, LLM decision (`_decide`), tick pipeline; module-level `heartbeat_service` singleton |
| [`scripts/core.py`](scripts/core.py) | HEARTBEAT.md management: `ensure_heartbeat_file_exists`, `add_task_to_heartbeat`, `list_active_tasks`, `list_completed_tasks`, `move_task_to_completed`, `remove_tasks_from_completed` / `clear_completed_tasks` |
| [`scripts/evaluate.py`](scripts/evaluate.py) | `evaluate_response()`: notification gate deciding whether a result is worth delivering |
| [`server/service/heartbeat.py`](../../../../server/service/heartbeat.py) | Integration: `process_heartbeat_task` (execution agent), `process_heartbeat_notify` (channel delivery), file read/write helpers |
| [`server/trigger/channels/core.py`](../../../../server/trigger/channels/core.py) | Wires `on_execute` / `on_notify` and starts the service on the channel manager's event loop |

---

## The HEARTBEAT.md File

- Located at `workspace/HEARTBEAT.md` — `HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"` in [`config/path.py`](../../../../config/path.py).
- If the file does not exist, `ensure_heartbeat_file_exists()` copies the language-independent template `workspace/template/HEARTBEAT.md` over it.
- Skeleton format (as shipped in `workspace/HEARTBEAT.md`):

```markdown
# Heartbeat Tasks

## Active Tasks

## Completed
```

Parsing rules (implemented in `scripts/core.py`):
- Sections are located by **exact line match** on `## Active Tasks` / `## Completed` (a section not found raises `ValueError`).
- A section's **content lines** are the non-blank lines that do not start with `<!--` (HTML comments), counted up to the next `##` heading or end of file.
- Tasks are Markdown list items; `add_task_to_heartbeat()` prefixes `- [ ] ` to any text that does not already start with `-`.
- The server write API additionally enforces `HEARTBEAT_MAX_CONTENT_LENGTH = 2000` characters of task text (headings, blank lines, and the `- ` markers are not counted, mirroring `heartbeat_content_length()`).

---

## Workflow

```
start() → asyncio task
   └─ loop: sleep(backoff.current_interval) → tick()   # first tick happens after one full interval
        ↓
   Read HEARTBEAT.md (empty/missing → skip tick)
        ↓
   _decide() — auxiliary LLM, virtual tool call:
     ├─ "skip" → log OK, wait for next tick
     └─ "run"  → on_execute(tasks)         # server: one-shot main-LLM agent
                    ↓
              response non-empty → evaluate_response():
                ├─ True  → on_notify(response)   # server: channel delivery
                └─ False → silenced (logged)

tick failure → backoff.record_failure(): next sleep doubles (interval_s × 2ⁿ,
capped at 7200 s); after 5 consecutive failures the loop exits (CRITICAL log).
Clean tick → backoff.record_success(): full reset to interval_s.
```

### Phase 1: Read

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

- Empty file → the tick is skipped (debug log).
- Missing file → `read_text()` raises `FileNotFoundError`; the loop logs the error and continues with the next interval. This is **not** recorded as a backoff failure (the file read sits outside the tick's backoff-accounted `try/except`).

### Phase 2: Decision (`_decide`)

The auxiliary LLM (`build_auxiliary_llm()` from `models`) receives the current time (`current_time_str(self.timezone)`) and the full HEARTBEAT.md content, and answers through a **virtual tool call** — avoiding unreliable free-text parsing:

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "description": "Report heartbeat decision after reviewing tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["skip", "run"],
                    "description": "skip = nothing to do, run = has active tasks",
                },
                "tasks": {
                    "type": "string",
                    "description": "Natural-language summary of active tasks (required for run)",
                },
            },
            "required": ["action"],
        },
    },
}]
```

- `bind_tools` path first; an empty `tool_calls` list is treated as `skip`.
- On `NotImplementedError` (e.g. a local GGUF branch without tool support) or any other exception, it falls back to `with_structured_output(_HeartbeatDecision)` (a Pydantic model whose `action` field is constrained by the pattern `^(skip|run)$`). If that also fails, the decision defaults to `("skip", "")`.

### Phase 3: Execute & Notification Gate

Actual `_tick()` logic (`scripts/base.py`):

```python
action, tasks = self._decide(content)
if action != "run":
    return  # "Heartbeat: OK (nothing to report)"

if self.on_execute:
    response: str = await self.on_execute(tasks)
    if response:
        should_notify: bool = evaluate_response(response, tasks)
        if should_notify and self.on_notify:
            await self.on_notify(response)
        # else: "Heartbeat: silenced by post-run evaluation"
```

- `run` → `on_execute(tasks)` runs the task; only a **non-empty** response is evaluated by `evaluate_response()`; only a positive verdict reaches `on_notify()`.
- An exception inside the tick is logged (`logger.exception`) and recorded as a **backoff failure**: the next sleep doubles (interval_s × 2ⁿ, capped at 7200 s) and the failure reason is kept. A clean tick fully resets the backoff.
- After **5 consecutive failures** the loop stops itself with a CRITICAL log ("Heartbeat paused ... manual recovery required"). Only a process restart resumes the schedule; `trigger_now()` still fires one-shot ticks. See [`runtime/periodic_backoff.py`](../../../../runtime/periodic_backoff.py) and the [loop-prevention harness doc](../../../../docs/harness/loop-prevention/README.md).

---

## HEARTBEAT.md Task Management API (`scripts/core.py`)

Agent-facing functions, exposed to the model through [SKILL.md](SKILL.md):

| Function | Behavior |
|---|---|
| `ensure_heartbeat_file_exists()` | Copies `workspace/template/HEARTBEAT.md` → `workspace/HEARTBEAT.md` if the file does not exist |
| `add_task_to_heartbeat(task_text, index=None)` | Adds a task under `## Active Tasks`; non-bullet text gets a `- [ ] ` prefix; `index` is a 0-based position among the section's content lines (`IndexError` when out of range); `None` appends at the end |
| `list_active_tasks()` / `list_completed_tasks()` | Return the content lines of `## Active Tasks` / `## Completed` |
| `move_task_to_completed(task_text)` | Substring match (after stripping) against Active Tasks lines; removes the first match and appends it at the end of `## Completed` (right after the heading if that section is empty); no match → `ValueError` |
| `remove_tasks_from_completed(task_text=None)` | `None` → remove **all** content lines; `str` / `list[str]` → substring match, remove matching lines (zero matches → `ValueError`); consecutive blank lines in the section are compacted afterwards |
| `clear_completed_tasks(task_text=None)` | Alias for `remove_tasks_from_completed` |

All of these are exported from `skills.builtin.core.heartbeat.scripts`; the package `skills.builtin.core.heartbeat` itself re-exports only the `heartbeat_service` singleton.

---

## Server Integration

The service is wired and started by the channel layer in `server/trigger/channels/core.py`:

```python
heartbeat_service.on_execute = _process_heartbeat_task   # → server.service.process_heartbeat_task
heartbeat_service.on_notify = _process_heartbeat_notify  # → server.service.process_heartbeat_notify
asyncio.run_coroutine_threadsafe(heartbeat_service.start(), event_loop)  # channel manager loop
```

**Execution (`process_heartbeat_task`)**:
1. `ensure_workspace_system_files()` guarantees the core persona files exist.
2. Builds a one-shot `create_agent(model=build_main_llm(), tools=[python_repl, read_file, write_file])` with the core persona system prompt (`build_system_prompt(selected_file_names=CORE_SYSTEM_FILE_NAMES)`) and the task summary as a `HumanMessage`.
3. Takes the last message content as the result.
4. Moves executed tasks Active → Completed: `move_task_to_completed(task)` first; on `ValueError` (task text drifted) it falls back to moving **all** remaining active tasks.
5. Pushes two best-effort WebSocket events to session `default`: `heartbeat:updated` (refreshed file content) and `notification` (result prefixed with `heartbeat: `). Failures are logged, never raised. On an internal exception the function returns `"Error occurred: {e}"` instead.

Note the layering: the WebSocket events above are pushed by `process_heartbeat_task` after **every** successful run, while **channel delivery** (below) is what the `evaluate_response()` gate actually controls.

**Delivery (`process_heartbeat_notify`)**: reads `plugins/channels/config.json`; every channel whose config has `"heartbeat": true` and a resolvable `receiver` (from `plugins/channels/<name>/config.json`, falling back to the root block) receives the result via `channel_manager.get_channel(name).send(OutboundMessage(...))`.

**HTTP API** (`server/trigger/http/heartbeat.py`): `GET /heartbeat` returns `{"HEARTBEAT.md": "<content>"}` (empty dict if the file is missing); `PUT /heartbeat` accepts `{"file_to_content": {"HEARTBEAT.md": "..."}}` and enforces the 2000-character task-text cap.

---

## Usage Examples

### Basic Usage (singleton)

```python
from skills.builtin.core.heartbeat import heartbeat_service

heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier        # async (response: str) -> None

await heartbeat_service.start()  # default interval: 1800 s (30 min)
```

In production this wiring lives in `server/trigger/channels/core.py`, which runs it on the channel manager's event loop.

### Manual Trigger

```python
result = await heartbeat_service.trigger_now()
```

`trigger_now()` reads the file, runs `_decide`, and — on `run` — awaits `on_execute(tasks)`. It **does not** run the notification gate and **does not** call `on_notify`; it returns `None` when the file is empty, the decision is `skip`, or `on_execute` is unset.

### Custom Configuration

```python
from skills.builtin.core.heartbeat.scripts.base import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 minutes
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

(The `HeartbeatService` class is defined in `scripts/base.py`; it is not re-exported by the package `__init__.py`.)

### Stop

```python
heartbeat_service.stop()  # sets _running = False and cancels the asyncio task
```

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `interval_s` | `30 * 60` (1800 s) | Seconds between ticks; the loop sleeps **before** each tick, so the first check happens one interval after `start()`. Also the base interval of the failure backoff |
| Failure backoff | `factor=2.0`, cap `7200 s`, stop after `5` | Hardcoded `PeriodicBackoff` parameters (`HeartbeatService.__init__`, `runtime/periodic_backoff.py`); consecutive tick failures stretch the sleep up to 2 h, then the service stops until restart |
| `enabled` | `True` | When `False`, `start()` logs "Heartbeat disabled" and does nothing |
| `timezone` | `None` | Passed to `current_time_str()` for the "Current Time" line of the decision prompt |
| `on_execute` / `on_notify` | `None` | Async callbacks; execution / delivery are skipped when unset |
| `HEARTBEAT_PATH` | `workspace/HEARTBEAT.md` | Defined in `config/path.py` |
| `HEARTBEAT_TEMPLATE_PATH` | `workspace/template/HEARTBEAT.md` | Source for `ensure_heartbeat_file_exists()` |
| `HEARTBEAT_MAX_CONTENT_LENGTH` | `2000` | Task-text cap enforced by the server write API (`write_heartbeat_file`) |

---

## Notification Gate Strategy

`evaluate_response(response, task_context)` in `scripts/evaluate.py` asks the auxiliary LLM through a virtual `evaluate_notification` tool (`should_notify` boolean, required; `reason` string). Its system prompt:

| Notify (`should_notify: true`) | Suppress (`should_notify: false`) |
|--------------------------------|-----------------------------------|
| Actionable information | Routine status check with nothing new |
| Errors | Confirmation that everything is normal |
| Completed deliverables | Essentially empty response |
| Anything the user explicitly asked to be reminded about | |

Failure behavior: no tool call returned, or any exception → **`True` (notify)**, so important messages are never silently dropped. Unlike `_decide`, there is no `with_structured_output` fallback.

---

## Not to Be Confused: `HeartbeatStaleness` Middleware

[`agent/middlewares/heartbeat_staleness.py`](../../../../agent/middlewares/heartbeat_staleness.py) shares the "heartbeat" name but is a **different subsystem**: a per-turn watchdog for stuck agent turns. It starts a 1-minute timer via `timer_call_register` in `before_agent`, tracks `(heartbeat_iter, heartbeat_tool)` progress, and after 7 stale cycles while idle or 20 cycles while inside a tool marks the turn as killed, making the next model/tool call raise `HeartbeatTimeoutError`. It does not read HEARTBEAT.md and is not part of this service.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python asyncio (single `asyncio.Task` loop via `asyncio.create_task`) |
| Decision & gate | Auxiliary LLM via `build_auxiliary_llm()` (`models`), LangChain virtual tool calls (`bind_tools`); `with_structured_output` fallback in `_decide` |
| File I/O | `pathlib` |
| Logging | `loguru` |
| Validation | Pydantic (`_HeartbeatDecision` fallback model) |
| Paths | `config.path` (`HEARTBEAT_PATH`, `HEARTBEAT_TEMPLATE_PATH`) |
