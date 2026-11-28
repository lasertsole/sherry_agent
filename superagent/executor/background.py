"""Background job ownership (prototype ``src/executor/background.ts``).

Assignment model A: a worker subagent is goal-bounded and drains on finish.
Shell jobs may outlive that turn; their output is read by the next wake, not
by pushing a new turn into a finished subagent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC

SHELL_JOB_LIMIT = timedelta(minutes=30)


@dataclass
class Job:
    """One background job owned by a Ticket."""

    id: str
    ticket_id: str
    kind: str  # shell | subagent
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_id: str = ""

    def expired(self, now: datetime | None = None) -> bool:
        """Return whether a shell job passed its 30-minute limit."""
        if self.kind != "shell":
            return False
        return (now or datetime.now(UTC)) - self.started_at >= SHELL_JOB_LIMIT


class BackgroundWork:
    """Track and stop background work for one Runtime."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def track(self, job: Job) -> None:
        """Register a job."""
        self._jobs[job.id] = job

    def running_jobs(self, ticket_id: str | None = None) -> list[Job]:
        """Return jobs, optionally filtered by Ticket."""
        return [
            job for job in self._jobs.values() if ticket_id is None or job.ticket_id == ticket_id
        ]

    def expired_shell_jobs(self, now: datetime | None = None) -> list[Job]:
        """Return shell jobs that must be killed."""
        return [job for job in self._jobs.values() if job.expired(now)]

    def kill_jobs(self, ticket_id: str | None = None) -> list[str]:
        """Remove shell jobs; return their ids for the caller to terminate."""
        killed: list[str] = []
        for job in list(self._jobs.values()):
            if ticket_id is not None and job.ticket_id != ticket_id:
                continue
            if job.kind == "shell":
                killed.append(job.id)
                self._jobs.pop(job.id, None)
        return killed

    def stop_subagents(self, ticket_id: str) -> list[str]:
        """Remove worker subagent jobs for a Ticket."""
        run_ids: list[str] = []
        for job in list(self._jobs.values()):
            if job.ticket_id == ticket_id and job.kind == "subagent":
                run_ids.append(job.run_id or job.id)
                self._jobs.pop(job.id, None)
        return run_ids

    def stop_background(self, ticket_id: str) -> tuple[list[str], list[str]]:
        """Stop all background work for a Ticket; return (shell, subagents)."""
        return self.kill_jobs(ticket_id), self.stop_subagents(ticket_id)

    def holds_slot(self, ticket_id: str) -> bool:
        """Return whether a live subagent still justifies holding a slot."""
        return any(
            job.ticket_id == ticket_id and job.kind == "subagent" for job in self._jobs.values()
        )


__all__ = ["BackgroundWork", "Job", "SHELL_JOB_LIMIT"]
