"""Worker-Session orchestrator tool backends (prototype ``src/app/tools/executor.ts``)."""

from __future__ import annotations

from typing import Any

from superagent.core.model import Report, ReportKind, SourceRef
from superagent.app.tools.tool import tools_for_role


def ack(engine: Any, args: dict[str, Any]) -> None:
    """Acknowledge delivered Inputs."""
    engine.inputs_read(
        str(args.get("ticket_id", "")),
        [str(item) for item in args.get("input_ids", [])],
    )


def report(engine: Any, args: dict[str, Any]) -> None:
    """Report a worker outcome; engine enforces Ticket scoping."""
    engine.report(
        str(args.get("ticket_id", "")),
        Report(
            assignment_id=str(args.get("assignment_id", "")),
            kind=ReportKind(str(args.get("kind", "progress"))),
            summary=str(args.get("summary", ""))[:600],
            at=str(args.get("at", "")),
            question=args.get("question"),
            pull_request=args.get("pull_request"),
        ),
    )


def mark_unrelated(engine: Any, args: dict[str, Any]) -> None:
    """Release a context-only follow."""
    engine.mark_unrelated(
        str(args.get("ticket_id", "")),
        SourceRef(source=str(args.get("source", "")), id=str(args.get("id", ""))),
        args.get("reason", {}),
    )


def follow(engine: Any, args: dict[str, Any]) -> None:
    """Follow a Thread for context only."""
    engine.follow_for_context(
        str(args.get("ticket_id", "")),
        SourceRef(source=str(args.get("source", "")), id=str(args.get("id", ""))),
    )


def handback(engine: Any, args: dict[str, Any]) -> bool:
    """Hand a follow-up back for a fresh Eval."""
    return engine.handback(
        str(args.get("ticket_id", "")),
        str(args.get("trigger_id", "")),
        args.get("reason", {}),
    )


HANDLERS: dict[str, Any] = {
    "worker.ack": ack,
    "worker.report": report,
    "worker.unrelated": mark_unrelated,
    "worker.follow": follow,
    "worker.handback": handback,
}


def tool_names() -> list[str]:
    """Return the worker role's tool names."""
    return [tool.name for tool in tools_for_role("worker")]


__all__ = ["HANDLERS", "tool_names"]
