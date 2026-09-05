# EMA Cron — Scheduled Task Service

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

A lightweight, file-based cron service for scheduling and executing one-shot, interval, and cron-expression agent tasks in the EMA AI Agent system. Jobs are persisted to `cron_jobs.json` in the project root, executed by a dedicated background service, and their results are delivered through the message bus to enabled channels.

## Features

- Three schedule types: `at` (one-shot), `every` (fixed interval), `cron` (cron expression via `croniter`)
- File-based persistence in `cron_jobs.json` (project root) with auto-reload when the file is modified externally
- A dedicated background service thread with its own asyncio event loop; a self-re-arming timer wakes up exactly at the earliest due job
- Job execution runs a dedicated agent (main LLM + system prompt + Python REPL / read-file / write-file tools)
- Results are delivered to channels through the `MessageBus` inbound queue, plus a best-effort WebSocket `notification` event to the browser UI
- Per-job execution logs written as JSON Lines to `logs/output/cron/<job_id>.log`
- Protected system jobs (`payload.kind == "system_event"`) cannot be removed
- Timezone support for cron expressions (IANA names via `zoneinfo`)
- REST API (Robyn) at `GET/POST/PUT/DELETE /cron`, `POST /cron/trigger`, `POST /cron/enable`, `POST /cron/failure-state`, `POST /cron/reset-failures` for the desktop client

## Module Structure

```
skills/builtin/core/cron/
├── __init__.py
├── SKILL.md             # Agent skill definition (add / list / remove / set_context recipes)
└── scripts/
    ├── __init__.py      # Public exports: CronService, cron_service, Cron, cron, types
    ├── base.py          # CronService singleton, cron_jobs.json I/O, timer loop, job execution
    ├── core.py          # Cron facade (agent-facing): add_job / list_jobs / remove_job / set_context
    ├── types.py         # Data models: CronSchedule, CronPayload, CronRunRecord, CronJobState, CronJob, CronStore
    └── README.md        # This file
```

Related code outside this skill:

- [`server/trigger/http/cron.py`](../../../../../server/trigger/http/cron.py) — REST endpoints wrapping `cron_service`
- [`../SKILL.md`](../SKILL.md) — how the agent invokes the skill scripts
- `cron_jobs.json` — job store at the project root (`config.ROOT_DIR / "cron_jobs.json"`)
- `logs/output/cron/` — per-job execution logs

## How It Works

1. **Service startup**: the service entry point calls `init()` in `skills.builtin.core.cron.scripts.base`, which wires the execution callback and starts a daemon thread named `cron-service` (`_start_cron_service_thread`). The thread creates a dedicated asyncio event loop, runs `cron_service.start()`, then loops forever. Importing the cron scripts is side-effect-free; `CronService.add_job()` / `register_system_job()` also lazily auto-start the service on the caller's event loop if it is not running yet.
2. **Timer loop**: `_arm_timer()` schedules one `asyncio` sleep until the earliest `nextRunAtMs` among enabled jobs; `_on_timer()` then reloads the store (picking up external edits), executes every enabled job whose `nextRunAtMs <= now`, saves the store, and re-arms the timer.
3. **Execution** (`_execute_job`): the callback registered via `set_on_job` (i.e. `_on_cron_job`) runs the job; the job's `lastStatus` / `lastError` are recorded, a WS notification is pushed, and an execution log line is appended. One-shot (`at`) jobs are then deleted (if `deleteAfterRun`) or disabled; recurring jobs get their next run time recomputed.

**Result delivery** (`_on_cron_job` in `base.py`):

1. A fresh agent is built with `create_agent(system_prompt=build_system_prompt(), model=build_main_llm(), tools=[build_python_repl_tool(), build_read_file_tool(), build_write_file_tool()])` and invoked with the job's `payload.message` as a `HumanMessage`.
2. The agent's final message is published to the message bus as `InboundMessage(channel=payload.channel, sender_id="cron tool", chat_id=payload.to, content=result)`.
3. The channel inbound consumer (`server/trigger/channels/core.py`) processes that message per enabled channel and delivers the generated reply via `channel.send(OutboundMessage(...))` to the configured `chat_id`.
4. Independently, `_push_cron_notification` sends `{"event": "notification", "content": "cron: <job name> [<status>]"}` over the WebSocket of session `default` (`CRON_WS_SESSION_ID`) so the browser UI notification bell updates live. Best-effort: failures are logged and never break the flow.

> Note: the `deliver` field is stored on the job and exposed via the API, but the current execution path (`_on_cron_job`) publishes the result to the bus regardless of it. Whether a message actually reaches a user depends on the enabled channels (see `plugins/channels/config.json`).

## Job Store (`cron_jobs.json`)

Jobs are persisted to `cron_jobs.json` in the project root. The file is loaded on service start and automatically reloaded whenever its modification time changes — you can edit it directly to batch-add or batch-modify jobs; changes take effect on the next timer tick.

On disk, fields use camelCase (`_save_store` / `_load_store` in `base.py`). Top level: `version` (int) and `jobs` (array). Example job:

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily_digest",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "atMs": null,
        "everyMs": null,
        "expr": "0 9 * * *",
        "tz": "Asia/Shanghai"
      },
      "payload": {
        "kind": "agent_turn",
        "message": "Summarize today's schedule and important events",
        "deliver": false,
        "channel": null,
        "to": null
      },
      "state": {
        "nextRunAtMs": 1756000000000,
        "lastRunAtMs": null,
        "lastStatus": null,
        "lastError": null
      },
      "createdAtMs": 1755000000000,
      "updatedAtMs": 1755000000000,
      "deleteAfterRun": false
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Unique job ID (first 8 chars of a `uuid4`) |
| `name` | `str` | Human-readable name |
| `enabled` | `bool` | Whether the job is active (default `true`) |
| `schedule` | `object` | When to run: see below |
| `payload` | `object` | What to run: see below |
| `state` | `object` | Runtime state: see below |
| `createdAtMs` | `int` | Creation timestamp (ms) |
| `updatedAtMs` | `int` | Last-update timestamp (ms) |
| `deleteAfterRun` | `bool` | Delete the job after a one-shot run (default `false`) |

**`schedule`**

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"at" \| "every" \| "cron"` | Schedule type |
| `atMs` | `int \| null` | Unix timestamp (ms) — for `kind: "at"` |
| `everyMs` | `int \| null` | Interval in ms — for `kind: "every"` |
| `expr` | `str \| null` | Cron expression, e.g. `"0 9 * * *"` — for `kind: "cron"` |
| `tz` | `str \| null` | IANA timezone, e.g. `"Asia/Shanghai"` — only valid with `kind: "cron"` |

**`payload`**

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"agent_turn" \| "system_event"` | Payload type (default `"agent_turn"`; jobs added through the service or API are always `agent_turn`) |
| `message` | `str` | Prompt message sent to the agent |
| `deliver` | `bool` | Delivery flag (default `false`; see note above — not consulted by the current execution path) |
| `channel` | `str \| null` | Channel name, e.g. `"qq"` |
| `to` | `str \| null` | Recipient identifier (used as `chat_id`) |

**`state`**

| Field | Type | Description |
|-------|------|-------------|
| `nextRunAtMs` | `int \| null` | Next scheduled run (ms); `null` for disabled/expired jobs |
| `lastRunAtMs` | `int \| null` | Last execution start time (ms) |
| `lastStatus` | `"ok" \| "error" \| "skipped" \| null` | Last execution outcome |
| `lastError` | `str \| null` | Last error message |

Python-side equivalents (`types.py`) use snake_case (`at_ms`, `every_ms`, `next_run_at_ms`, `last_run_at_ms`, `last_status`, `last_error`, `created_at_ms`, `updated_at_ms`, `delete_after_run`). `CronRunRecord` is exported but currently unused.

## Public API

### Agent skill commands (`Cron` facade, singleton `cron` in `core.py`)

These are the commands exposed to the agent via [`../SKILL.md`](../SKILL.md), used as `from skills.builtin.core.cron.scripts import cron`:

| Command | Description |
|---------|-------------|
| `cron.set_context(channel, chat_id)` | Set the session context (both required, non-empty) used as delivery target for subsequently added jobs |
| `cron.add_job(name=None, message, every_seconds=None, cron_expr=None, tz=None, at=None, deliver=True)` | Add a job. Exactly one of `every_seconds` / `cron_expr` / `at` (ISO datetime) is required. Requires prior `set_context`. `tz` is only valid with `cron_expr` (defaults to `"UTC"`); naive `at` datetimes are assumed UTC; `at` jobs get `delete_after_run=True`; `name` defaults to the first 30 chars of `message` |
| `cron.list_jobs()` | Human-readable listing: timing, purpose and protected flag for system jobs, last/next run times |
| `cron.remove_job(job_id)` | Remove a job; returns friendly errors for protected system jobs |

### `CronService` (Python API, singleton `cron_service` in `base.py`)

| Method | Description |
|--------|-------------|
| `await start()` | Load the store, recompute next runs, save, and arm the timer |
| `stop()` | Stop the service and cancel the timer task |
| `set_on_job(callback)` | Register the async execution callback (wired to `_on_cron_job` by `init()`) |
| `list_jobs(include_disabled=False)` | List jobs sorted by next run time; disabled jobs only when `include_disabled=True` |
| `add_job(name, schedule, message, deliver=False, channel=None, to=None, delete_after_run=False)` | Add a job (`payload.kind` is always `"agent_turn"`); auto-starts the service; returns the `CronJob` |
| `register_system_job(job)` | Idempotently (re-)register a system job by `id` (no in-repo callers at the moment) |
| `remove_job(job_id)` | Returns `"removed"`, `"protected"` (`payload.kind == "system_event"`), or `"not_found"` |
| `enable_job(job_id, enabled=True)` | Enable/disable; recomputes or clears `nextRunAtMs` |
| `await run_job(job_id, force=False)` | Run now; disabled jobs are skipped unless `force=True` |
| `get_job(job_id)` | Get a job by ID or `None` |
| `status()` | `{"enabled": bool, "jobs": int, "next_wake_at_ms": int \| None}` |

### HTTP REST API (`server/trigger/http/cron.py`, backend at `http://127.0.0.1:8080`)

| Endpoint | Description |
|----------|-------------|
| `GET /cron?include_disabled=false` | List jobs (camelCase JSON) |
| `POST /cron` | Create: `{"name", "message", "schedule": {"kind", "atMs"/"everyMs"/"expr"/"tz"}, "deliver", "channel", "to", "delete_after_run"}` |
| `PUT /cron` | Update: applied as remove + re-add while preserving `id` and `createdAtMs` |
| `POST /cron/trigger` | Run now: `{"id", "force"}` (400 if disabled and no `force`) |
| `POST /cron/enable` | Enable/disable: `{"id", "enabled"}` |
| `POST /cron/failure-state` | Inspect the failure breaker state: `{"id"}` → `{consecutive_failures, last_error, degraded_since, backoff_ms}`; unknown job → `404`, never-failed job → zeroed state |
| `POST /cron/reset-failures` | Reset the failure breaker state: `{"id"}`; re-enables only jobs the breaker itself disabled (operator disables preserved) |
| `DELETE /cron` | Remove: `{"id"}`; `403` for protected system jobs |

## Failure Breaker

Every recurring job is guarded by a per-job failure breaker (`CronJobFailureState` in `base.py`): consecutive failures escalate from degrade to auto-disable, so a permanently broken job cannot fire forever. The breaker state is memory-only; `cron_jobs.json` keeps its schema, and the only thing the breaker ever writes back is the job's pre-existing `enabled` flag.

| Consecutive failures | Effect |
|----------------------|--------|
| 1-4 | Job fails normally: status marked error, WS bell notification |
| ≥ 5 (degraded) | Triggers are skipped while inside the backoff window: `min(5000ms × 2^(n-5), 300000ms)` since the last failure (n = consecutive failure count) |
| ≥ 10 | `enabled=False` is persisted to the job store; a best-effort notification is sent to the job's payload channel |

- **Record-then-re-raise:** the failure is recorded first, then the exception is re-raised, so `lastStatus` / `lastError` reporting and the WS bell stay intact.
- **One-shot `at` jobs are exempt** (they can never fire twice, so a single failure is not a loop).
- Success resets the state completely; a manual `enable_job` clears it. `POST /cron/reset-failures` re-enables only jobs the breaker itself disabled, so operator disables are preserved.
- The in-memory counters do not survive a process restart (the persisted `enabled` flag does).

## Usage Examples

Agent skill script (see [`../SKILL.md`](../SKILL.md)):

```python
from loguru import logger
from skills.builtin.core.cron.scripts import cron

# Context must be set once per session before adding jobs
cron.set_context(channel="qq", chat_id="group_123456")

# Cron-expression job: 9 AM daily, Shanghai time
res = cron.add_job(
    name="daily_digest",
    message="Summarize today's schedule and important events",
    cron_expr="0 9 * * *",
    tz="Asia/Shanghai",
)
logger.info(res)

# Interval job: every 30 minutes
res = cron.add_job(
    message="Check today's weather and remind user to bring an umbrella if needed",
    every_seconds=30 * 60,
)

# One-shot job: explicit ISO datetime
res = cron.add_job(message="Say good morning to the user", at="2026-02-12T10:30:00")

logger.info(cron.list_jobs())
# cron.remove_job("a1b2c3d4")
```

Python API:

```python
from skills.builtin.core.cron.scripts import cron_service, CronSchedule

# The service auto-starts on first use; an explicit start is optional
await cron_service.start()

job = cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

jobs = cron_service.list_jobs()
print([j.name for j in jobs])

await cron_service.run_job(job.id, force=True)   # manual trigger
cron_service.remove_job(job.id)                   # "removed" | "protected" | "not_found"
```

HTTP:

```bash
curl http://127.0.0.1:8080/cron
curl -X POST http://127.0.0.1:8080/cron -H "Content-Type: application/json" \
  -d '{"name": "daily_digest", "message": "Summarize today", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}}'
curl -X POST http://127.0.0.1:8080/cron/trigger -H "Content-Type: application/json" -d '{"id": "a1b2c3d4", "force": true}'
```

## Scheduling Semantics

| Kind | Behavior |
|------|----------|
| `at` | Fires once at `atMs`. If the timestamp is already in the past when computed, `nextRunAtMs` becomes `null` and the job never fires. After a run it is deleted (`deleteAfterRun=true`) or disabled (`enabled=false`, `nextRunAtMs=null`) |
| `every` | Next run = current time + `everyMs`, recomputed after each execution |
| `cron` | `croniter` computes the next run from the expression; the base time is evaluated in `tz` if set, otherwise in the system's local timezone |

Validation: `tz` is only accepted with `kind: "cron"`; unknown IANA timezone names are rejected (`ValueError`) in both the service and the facade.

## Protected System Jobs

Jobs with `payload.kind == "system_event"` are protected: `CronService.remove_job()` refuses them (`"protected"`, HTTP `DELETE /cron` → `403`). The skill layer additionally recognizes the `dream` job by name and describes it as the Dream memory-consolidation job for long-term memory. Jobs added through `add_job` (Python, skill, or HTTP) are always `agent_turn`; `system_event` jobs can only come from `register_system_job()` or direct edits to `cron_jobs.json`.

## Dependencies

- `croniter>=6.2.2` — cron expression parsing
- Python `zoneinfo` — timezone support
- No cron-specific configuration knobs exist in `config/`; there are no cron-related environment variables

## Notes

- Execution history: each run appends one JSON line to `logs/output/cron/<job_id>.log` with `timestamp`, `job_id`, `job_name`, `start_time`, `end_time`, `duration_ms`, `status`, `error`, `message`. There is no in-memory run history (`CronRunRecord` is unused legacy).
- External edits to `cron_jobs.json` are detected by file modification time and picked up on the next timer tick; the store is re-saved after every execution.
- The service runs on its own event loop in the `cron-service` daemon thread, independent of the main server loop; `run_job()` and `start()` must be awaited from a running loop.
- The WebSocket notification targets session `"default"` (the browser client session), so desktop notifications only arrive while a client is connected.

▶️ Full details: [docs/harness/loop-prevention/README.md](../../../../../docs/harness/loop-prevention/README.md) · [中文](../../../../../docs/harness/loop-prevention/README.zh.md) · [한국어](../../../../../docs/harness/loop-prevention/README.ko.md) · [日本語](../../../../../docs/harness/loop-prevention/README.ja.md)
