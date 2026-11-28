"""Sherry session seam (prototype ``src/app/sessions.ts``).

All Sherry session operations go through the loopback client port.  A degraded
backend raises directly so callers can decide to keep queueing Triggers.
"""

from __future__ import annotations

from typing import Any, Protocol

from superagent.core.model import Report, ReportKind

MAIN_SESSION_KEY = "superagent-main"


class SessionClient(Protocol):
    """Loopback port for session operations."""

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, path: str) -> dict[str, Any]: ...


class Sessions:
    """Ensure, inject and heal the persistent main Session."""

    def __init__(self, client: SessionClient, *, main_key: str = MAIN_SESSION_KEY) -> None:
        self.client = client
        self.main_key = main_key
        self.degraded = False

    async def ensure(self) -> dict[str, Any]:
        """Create or reuse the persistent router Session."""
        result = await self.client.post(
            "/internal/superagent/session/ensure", {"session_key": self.main_key}
        )
        self.degraded = False
        return result

    async def exists(self) -> bool:
        """Return whether the main Session still exists in Sherry."""
        try:
            result = await self.client.post(
                "/internal/superagent/session/exists", {"key": self.main_key}
            )
        except Exception:  # noqa: BLE001 — backend down means degraded
            self.degraded = True
            return False
        return bool(result.get("exists", False))

    async def inject(self, text: str, *, channel: str = "candidate") -> dict[str, Any]:
        """Inject a candidate/notice message into the main Session."""
        return await self.client.post(
            "/internal/superagent/session/inject",
            {"session_key": self.main_key, "text": text, "channel": channel},
        )

    async def heal(self, session_key: str) -> dict[str, Any]:
        """Heal an interrupted turn after a process restart."""
        return await self.client.post(
            "/internal/superagent/session/heal", {"session_key": session_key}
        )

    async def add_messages(self, session_key: str, messages: list[str]) -> dict[str, Any]:
        """Deliver queued Inputs into a running worker Session."""
        return await self.client.post(
            "/internal/superagent/session/add_messages",
            {"session_key": session_key, "messages": messages},
        )

    async def truncate(self, session_key: str, after_turn: int) -> dict[str, Any]:
        """Truncate messages after a turn during a retry."""
        return await self.client.post(
            "/internal/superagent/session/truncate",
            {"session_key": session_key, "after_turn": after_turn},
        )

    async def read_state(self, session_key: str) -> dict[str, Any]:
        """Read the latest state snapshot of a Session."""
        return await self.client.get(f"/internal/superagent/session/state?key={session_key}")

    async def list_jobs(self, session_key: str) -> list[dict[str, Any]]:
        """List Sherry background jobs of a Session."""
        result = await self.client.get(f"/internal/superagent/session/jobs?key={session_key}")
        jobs = result.get("jobs", [])
        return [job for job in jobs if isinstance(job, dict)]

    async def kill_job(self, session_key: str, job_id: str) -> dict[str, Any]:
        """Kill one background job."""
        return await self.client.post(
            "/internal/superagent/session/kill_job",
            {"session_key": session_key, "job_id": job_id},
        )

    async def read_trajectory(self, session_key: str) -> list[dict[str, Any]]:
        """Read trajectory events for completion/relay handling."""
        result = await self.client.get(f"/internal/superagent/session/trajectory?key={session_key}")
        events = result.get("events", [])
        return [event for event in events if isinstance(event, dict)]


def hitl_report(question: str, at: str, assignment_id: str = "") -> Report:
    """Fail closed: a declined HITL question becomes blocked + Attention."""
    return Report(
        assignment_id=assignment_id,
        kind=ReportKind.BLOCKED,
        summary=question[:600],
        at=at,
        question=question,
    )


__all__ = ["MAIN_SESSION_KEY", "SessionClient", "Sessions", "hitl_report"]
