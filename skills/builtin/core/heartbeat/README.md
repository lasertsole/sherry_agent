# Heartbeat — Periodic Task Check Service

[**中文文档**](README.zh.md) | **English**

> **Heartbeat** is EMA AI Agent's periodic wake-up service that regularly checks `HEARTBEAT.md` for pending tasks and automatically executes and notifies.

---

## Motivation

After a conversation ends, the Agent may sit idle while external work remains:
- Background tasks awaiting results (async tool calls)
- Monitoring tasks that need periodic checks
- Long-running work that needs continued progress

Heartbeat provides a **lightweight polling mechanism** that enables the Agent to work proactively during idle periods.

---

## Architecture

```
┌─────────────────────────────────────┐
│          HeartbeatService            │
├─────────────────────────────────────┤
│  Loop (tick every N seconds)         │
│  ├─ Phase 1: Read HEARTBEAT.md       │
│  ├─ Phase 2: LLM decide (skip/run)   │
│  └─ Phase 3: Execute + notification gate │
└─────────────────────────────────────┘
```

### Module Responsibilities

| File | Responsibility |
|------|---------------|
| `core.py` | Main service: loop, LLM decision, task execution trigger |
| `evaluate.py` | Notification gate: decides whether results are worth delivering |

---

## Workflow

```
Timer fires (default 30 min)
     ↓
Read HEARTBEAT.md
     ↓
LLM (tool-call) decision:
  ├─ "skip" → no tasks, wait for next tick
  └─ "run" → execute via on_execute callback
                   ↓
              evaluate_response():
                ├─ True  → on_notify pushes result to user
                └─ False → silent (routine check, nothing new)
```

### Phase 1: Read

```python
content = Path(HEARTBEAT_PATH).read_text(encoding="utf-8")
```

`HEARTBEAT_PATH` is configured in `config.py`, pointing to the project's `HEARTBEAT.md`. If the file is missing or empty, the tick is skipped.

### Phase 2: Decision

Uses a **virtual tool-call** to let the LLM determine whether active tasks exist, avoiding unreliable free-text parsing:

```python
_HEARTBEAT_TOOL = [{
    "type": "function",
    "function": {
        "name": "heartbeat",
        "parameters": {
            "action": {"enum": ["skip", "run"]},
            "tasks": {"type": "string"},  # task summary when run
        },
        "required": ["action"],
    },
}]
```

`skip` → no operation; `run` → proceed to Phase 3.

### Phase 3: Execute & Notification Gate

```python
if action == "run" and self.on_execute:
    response = await self.on_execute(tasks)           # execute task
    should_notify = evaluate_response(response, tasks) # evaluate notification
    if should_notify and self.on_notify:
        await self.on_notify(response)                 # push to user
```

`evaluate_response()` uses an independent LLM tool-call to determine whether the response contains **actionable information** (errors, deliverables, user-requested results), suppressing routine status updates.

---

## Usage Examples

### Basic Usage

```python
from skills.builtin.core.heartbeat import heartbeat_service

# Configure callbacks
heartbeat_service.on_execute = my_task_executor  # async (tasks: str) -> str
heartbeat_service.on_notify = my_notifier  # async (response: str) -> None

# Start (default 30 min interval)
await heartbeat_service.start()
```

### Manual Trigger

```python
result = await heartbeat_service.trigger_now()
if result:
    print(f"Task result: {result}")
```

### Custom Configuration

```python
from skills.builtin.core.heartbeat import HeartbeatService

service = HeartbeatService(
    on_execute=my_executor,
    on_notify=my_notifier,
    interval_s=15 * 60,  # 15 minutes
    timezone="Asia/Shanghai",
    enabled=True,
)
await service.start()
```

### Stop

```python
heartbeat_service.stop()
```

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `interval_s` | 1800 (30 min) | Interval between ticks |
| `enabled` | True | Enable heartbeat service |
| `timezone` | None | Timezone for LLM decision (e.g. "Asia/Shanghai") |
| `HEARTBEAT_PATH` | see config.py | Path to HEARTBEAT.md |

---

## Notification Gate Strategy

`evaluate_response()` decision logic:

| Notify | Suppress |
|--------|----------|
| Errors or exceptions | Routine check, nothing unusual |
| Task deliverable completed | Confirmation that everything is normal |
| User explicitly requested info | Response is empty or irrelevant |

Defaults to `True` (notify) on failure, ensuring important messages are never silently dropped.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python asyncio |
| LLM Decision | `auxiliary_llm` (bind_tools) |
| File I/O | pathlib |
| Configuration | `config.HEARTBEAT_PATH` |
