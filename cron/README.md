# EMA Cron — Scheduled Task Service

[**中文文档**](README.zh.md) | **English**

A lightweight, file-based cron service module for scheduling and executing periodic, one-shot, and cron-expression-based agent tasks within the EMA AI Agent system.

## Features

- Three schedule types: `at` (one-shot), `every` (interval), `cron` (cron expression)
- File-based persistent storage (`jobs.json`) with auto-reload on external modification
- Timer-driven execution with millisecond precision
- Per-job run history (keeps last 20 records)
- Protected system jobs (cannot be removed via API)
- Timezone support for cron expressions
- Configurable message delivery to external channels (e.g. QQ, WhatsApp)

## Module Structure

```
cron/
├── __init__.py    # Public exports: CronService, cron_service, types
├── core.py        # Core implementation: CronService, job execution, timer loop
├── types.py       # Data models: CronSchedule, CronPayload, CronJob, etc.
├── jobs.json      # Persistent job store (auto-managed)
└── README.md      # This file
```

## Type Reference

### CronSchedule

Defines when a job should run.

| Field     | Type   | Description |
|-----------|--------|-------------|
| `kind`    | `"at" \| "every" \| "cron"` | Schedule type |
| `at_ms`   | `int \| None` | Unix timestamp in ms for "at" |
| `every_ms`| `int \| None` | Interval in ms for "every" |
| `expr`    | `str \| None` | Cron expression for "cron", e.g. `"0 9 * * *"` |
| `tz`      | `str \| None` | Timezone, e.g. `"Asia/Shanghai"`. Only for "cron" |

### CronPayload

Defines what action to take when the job fires.

| Field     | Type            | Description |
|-----------|-----------------|-------------|
| `kind`    | `"system_event" \| "agent_turn"` | Payload type |
| `message` | `str`           | Prompt message to send to the agent |
| `deliver` | `bool`          | Whether to deliver the result to an external channel |
| `channel` | `str \| None`   | Channel name (e.g. `"whatsapp"`, `"qq"`) |
| `to`      | `str \| None`   | Recipient identifier |

### CronJob

Complete job definition.

| Field             | Type            | Description |
|-------------------|-----------------|-------------|
| `id`              | `str`           | Unique job ID (auto-generated) |
| `name`            | `str`           | Human-readable name |
| `enabled`         | `bool`          | Whether the job is active |
| `schedule`        | `CronSchedule`  | Schedule definition |
| `payload`         | `CronPayload`   | Action definition |
| `delete_after_run`| `bool`          | Auto-delete after one-shot execution |

## Public API

### `CronService` (singleton via `cron_service`)

| Method | Description |
|--------|-------------|
| `start()` | Start the cron service |
| `stop()` | Stop the cron service |
| `list_jobs(include_disabled=False)` | List all jobs |
| `add_job(name, schedule, message, ...)` | Add a new job |
| `register_system_job(job)` | Register a protected system job |
| `remove_job(job_id)` | Remove a job |
| `enable_job(job_id, enabled=True)` | Enable or disable a job |
| `run_job(job_id, force=False)` | Manually trigger a job |
| `get_job(job_id)` | Get job details |
| `status()` | Get service status |

## Usage Examples

```python
from cron import cron_service, CronSchedule

# Start the service
await cron_service.start()

# One-shot job: run at a specific time
cron_service.add_job(
    name="morning_greeting",
    schedule=CronSchedule(kind="at", at_ms=1700000000000),
    message="Say good morning to the user",
    deliver=True,
    channel="qq",
    to="group_123456",
    delete_after_run=True,
)

# Interval job: run every 30 minutes
cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

# Cron job: run at 9 AM daily in Shanghai time
cron_service.add_job(
    name="daily_digest",
    schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
    message="Summarize today's schedule and important events",
)

# List all jobs
jobs = cron_service.list_jobs()
for j in jobs:
    print(f"{j.name}: next run at {j.state.next_run_at_ms}")

# Manually trigger a job
await cron_service.run_job("job_id_here", force=True)

# Remove a job
cron_service.remove_job("job_id_here")
```

## Job Persistence

All jobs are persisted in `jobs.json`. The file is auto-loaded on service start and auto-reloaded when external modifications are detected (by comparing file modification time). You can directly edit `jobs.json` to batch-add or batch-modify jobs — the service will pick up changes on the next tick.

## Scheduling Semantics

| Kind | Behavior |
|------|----------|
| `at` | Fires once at the specified timestamp. Disabled after run (or deleted if `delete_after_run=True`) |
| `every` | Re-fires at fixed `every_ms` interval from each completion |
| `cron` | Uses `croniter` to compute the next run time from the cron expression in the given timezone |

## Dependencies

- `croniter` — cron expression parsing
- Python `zoneinfo` — timezone support

## Notes

- One-shot (`at`) jobs are **disabled** (not deleted) after execution by default. Set `delete_after_run=True` to auto-delete.
- System jobs (`payload.kind == "system_event"`) are protected and cannot be removed via `remove_job()`.
- The cron service relies on the asyncio event loop — ensure your application is running an event loop when calling `await cron_service.start()`.
