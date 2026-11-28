"""Main-Session orchestrator tool backends (prototype ``src/app/tools/main.ts``)."""

from __future__ import annotations

from typing import Any

from superagent.core.model import DecisionOutcome, Reason, ReportKind, SourceRef
from superagent.app.tools.tool import tools_for_role


def candidate(engine: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return an assessment card for a Trigger."""
    return engine.candidate(str(args.get("trigger_id", "")))


def decide(engine: Any, args: dict[str, Any]) -> None:
    """Record an assessment decision."""
    outcome = DecisionOutcome(str(args.get("outcome", "ignore")))
    reason = Reason(str(args.get("reason", "assessment")), args.get("values", {}))
    engine.decide(str(args.get("trigger_id", "")), outcome, reason)


def retry(engine: Any, args: dict[str, Any]) -> None:
    """Retry a Ticket."""
    engine.retry_ticket(str(args.get("ticket_id", "")))


def request_status(engine: Any, args: dict[str, Any]) -> bool:
    """Ask the Executor for a Ticket status report."""
    return engine.request_status(str(args.get("ticket_id", "")))


def note(engine: Any, args: dict[str, Any]) -> None:
    """Add a context-only note."""
    engine.note(str(args.get("ticket_id", "")), str(args.get("text", "")))


def reply(engine: Any, args: dict[str, Any]) -> None:
    """Reply on a Ticket channel."""
    engine.reply(
        str(args.get("ticket_id", "")),
        ReportKind.PROGRESS,
        str(args.get("text", "")),
    )


def unsubscribe(engine: Any, args: dict[str, Any]) -> None:
    """Unsubscribe a Thread."""
    engine.unsubscribe(SourceRef(source=str(args.get("source", "")), id=str(args.get("id", ""))))


HANDLERS: dict[str, Any] = {
    "eval.candidate": candidate,
    "eval.decide": decide,
    "ticket.retry": retry,
    "ticket.status": request_status,
    "ticket.note": note,
    "ticket.reply": reply,
    "thread.unsubscribe": unsubscribe,
}


def tool_names() -> list[str]:
    """Return the main role's tool names."""
    return [tool.name for tool in tools_for_role("main")]


__all__ = ["HANDLERS", "tool_names"]
