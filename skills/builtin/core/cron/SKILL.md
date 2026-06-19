---
name: cron
description: 'Schedule reminders and recurring tasks. Actions: add, list, remove or set_context.'
---

## add a job to the cron
```python
from skills.builtin.core.cron.scripts import cron

if __name__ == '__main__':
    name: str| None = "{placeholder}"  # <- Optional short human-readable label for the job (e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message.
    message: str | None = "{placeholder}" # <- Reminder message (for add)
    every_seconds: int | None = int("{placeholder}") # <- Interval in seconds (for recurring tasks)
    cron_expr: str | None = "{placeholder}" # <- Cron expression like '0 9 * * *'
    tz: str | None = "{placeholder}" # <- IANA timezone (e.g. 'America/Vancouver')
    at: str | None = "{placeholder}" # <- ISO datetime (e.g. '2026-02-12T10:30:00')
    deliver: bool | None = bool("{placeholder}") #"Whether to deliver the execution result to the user channel (default false)"

    cron.add_job(name, message, every_seconds, cron_expr, tz, at, deliver)
```

## list jobs
```python
from skills.builtin.core.cron.scripts import cron

if __name__ == '__main__':
    cron.list_jobs()
```

## remove jobs
```python
from skills.builtin.core.cron.scripts import cron

if __name__ == '__main__':
    job_id: str | None = "{placeholder}"  # <- Job ID (for remove)
    cron.remove_job(job_id)
```

## set_context
```python
from skills.builtin.core.cron.scripts import cron

if __name__ == '__main__':
    channel: str = "{placeholder}" # <- Channel to send the execution result to, when chat_id is no empty, channel must be no empty (default None)
    chat_id: str = "{placeholder}" # <- Chat to send the execution result to, when channel is no empty, chat_id must be no empty (default None)

    cron.set_context(channel=channel, chat_id=chat_id)
```