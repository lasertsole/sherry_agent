"""Tool catalog shared by the MCP server (prototype ``src/app/tools/tool.ts``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    """One orchestrator tool exposed over MCP."""

    name: str
    description: str
    handler: str
    roles: frozenset[str] = frozenset({"main", "worker"})

    def allowed(self, role: str) -> bool:
        """Return whether a role may see this tool."""
        return role in self.roles


MAIN_TOOLS = (
    Tool("orchestrator_read", "Read a bounded Ticket view", "ticket.read"),
    Tool(
        "orchestrator_route", "Assess and route a candidate", "eval.candidate", frozenset({"main"})
    ),
    Tool("orchestrator_eval", "Read the assessment card", "eval.context", frozenset({"main"})),
    Tool("orchestrator_eval_read", "Expand a message stub", "eval.read", frozenset({"main"})),
    Tool(
        "orchestrator_eval_decide",
        "Record an assessment decision",
        "eval.decide",
        frozenset({"main"}),
    ),
    Tool("orchestrator_ticket_retry", "Retry a Ticket", "ticket.retry", frozenset({"main"})),
    Tool(
        "orchestrator_ticket_request_status",
        "Request a Ticket status report",
        "ticket.status",
        frozenset({"main"}),
    ),
    Tool("orchestrator_ticket_note", "Add a context note", "ticket.note", frozenset({"main"})),
    Tool(
        "orchestrator_trigger_requeue", "Requeue a Trigger", "trigger.requeue", frozenset({"main"})
    ),
    Tool("orchestrator_reply", "Reply on a Ticket channel", "ticket.reply", frozenset({"main"})),
    Tool(
        "orchestrator_thread_unsubscribe",
        "Unsubscribe a Thread",
        "thread.unsubscribe",
        frozenset({"main"}),
    ),
    Tool("orchestrator_settings_save", "Save settings", "settings.save", frozenset({"main"})),
    Tool("orchestrator_policy_sync", "Sync Source policy", "policy.sync", frozenset({"main"})),
    Tool("orchestrator_prompts_save", "Save prompts", "prompts.save", frozenset({"main"})),
)

WORKER_TOOLS = (
    Tool("orchestrator_ack", "Acknowledge an Input", "worker.ack", frozenset({"worker"})),
    Tool(
        "orchestrator_report", "Report progress or outcome", "worker.report", frozenset({"worker"})
    ),
    Tool(
        "orchestrator_thread_unrelated",
        "Mark a followed thread unrelated",
        "worker.unrelated",
        frozenset({"worker"}),
    ),
    Tool(
        "orchestrator_thread_follow",
        "Follow a thread for context",
        "worker.follow",
        frozenset({"worker"}),
    ),
    Tool(
        "orchestrator_trigger_handback",
        "Hand a follow-up back for assessment",
        "worker.handback",
        frozenset({"worker"}),
    ),
)

ALL_TOOLS = (*MAIN_TOOLS, *WORKER_TOOLS)

_BY_NAME = {tool.name: tool for tool in ALL_TOOLS}


def read_tool(name: str) -> Tool | None:
    """Look up a tool by name."""
    return _BY_NAME.get(name)


def tools_for_role(role: str) -> list[Tool]:
    """Return the tools visible to a role."""
    return [tool for tool in ALL_TOOLS if tool.allowed(role)]


__all__ = [
    "ALL_TOOLS",
    "MAIN_TOOLS",
    "WORKER_TOOLS",
    "Tool",
    "read_tool",
    "tools_for_role",
]
