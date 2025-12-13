from datetime import datetime
from langchain.tools import BaseTool
from cron.core import CronService, cron_service
from typing import Any, Optional, Type, Literal
from pydantic import BaseModel, Field, PrivateAttr
from cron.types import CronSchedule, CronJob, CronJobState


class CronInput(BaseModel):
    action: Literal["add", "list", "remove", "set_context"] = Field(..., description="Action to perform: 'add', 'list', 'remove', or 'set_context'(set receiver's channel and chat_id in context)")
    name: str = Field(None, description="Optional short human-readable label for the job (e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message.")
    message: Optional[str] = Field(None, description="Reminder message (for add)")
    every_seconds: Optional[int] = Field(None, description="Interval in seconds (for recurring tasks)")
    cron_expr: Optional[str] = Field(None, description="Cron expression like '0 9 * * *'")
    tz: Optional[str] = Field(None, description="IANA timezone (e.g. 'America/Vancouver')")
    at: Optional[str] = Field(None, description="ISO datetime (e.g. '2026-02-12T10:30:00')")
    job_id: Optional[str] = Field(None, description="Job ID (for remove)")
    deliver: bool = Field(None, description="Whether to deliver the execution result to the user channel (default true)")
    channel: Optional[str] = Field(None, description="Channel to send the execution result to, when chat_id is no empty, channel must be no empty")
    chat_id: Optional[str] = Field(None, description="Chat to send the execution result to, when channel is no empty, chat_id must be no empty")

class CronTool(BaseTool):
    name: str = "cron"
    description: str = "Schedule reminders and recurring tasks. Actions: add, list, remove or set_context."
    args_schema: Type[BaseModel] = CronInput

    _cron: CronService = PrivateAttr()
    _channel: str | None = PrivateAttr(default=None)
    _chat_id: str | None = PrivateAttr(default=None)
    _default_timezone: str = "UTC"

    def _set_context(self,  channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    def __init__(self, **data):
        super().__init__(**data)
        self._cron = cron_service

    def _run(self, **kwargs: Any) -> str:
        import asyncio
        return asyncio.run(self._arun(**kwargs))

    async def _arun(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        deliver: bool = True,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        match action:
            case "add":
                return self._add_job(name, message, every_seconds, cron_expr, tz, at, deliver)
            case "list":
                return self._list_jobs()
            case "remove":
                return self._remove_job(job_id)
            case "set_context":
                if (
                        channel is None
                        or channel == ""
                        or chat_id is None
                        or chat_id == ""
                ):
                    return "channel and chat_id are both required"
                self._set_context(channel=channel, chat_id=chat_id)
                return "Session context set"
            case _:
                return f"Unknown action: {action}"

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """Pick the most human-meaningful timezone for display."""
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    def _add_job(
        self,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        deliver: bool = True,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        job = self._cron.add_job(
            name=name or message[:30],
            schedule=schedule,
            message=message,
            deliver=deliver,
            channel=self._channel,
            to=self._chat_id,
            delete_after_run=delete_after,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        return "System-managed internal job."

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "Cannot remove job `dream`.\n"
                    "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                    "It remains visible so you can inspect it, but it cannot be removed."
                )
            return (
                f"Cannot remove job `{job_id}`.\n"
                "This is a protected system-managed cron job."
            )
        return f"Job {job_id} not found"

def build_cron_tool(session_id: str | None = None) -> CronTool:
    tool: CronTool = CronTool()
    tool.handle_tool_error = True
    return tool