"""HITL shared types, enums, constants, and configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_STATE_PREFIX = "hitl"

BLOCKED_MESSAGE = (
    "The user has NOT consented to this action. "
    "Do NOT retry this command, do NOT rephrase it, "
    "and do NOT attempt the same outcome via a different command."
)


class ApprovalMode(str, Enum):
    SMART = "smart"
    MANUAL = "manual"
    OFF = "off"


class ApprovalDecision(str, Enum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"


@dataclass
class ApprovalResult:
    approved: bool
    decision: ApprovalDecision | None = None
    reason: str = ""
    pattern_key: str = ""

    @property
    def blocked(self) -> bool:
        return not self.approved


class WriteTarget(str, Enum):
    MEMORY = "memory"
    SKILLS = "skills"


class TriageStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    TRIAGE = "triage"
    DONE = "done"


BLOCK_RECURRENCE_LIMIT = 3


class SmartApprovalResult(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    ESCALATE = "escalate"


# Lazy langchain/langgraph imports — may fail due to version mismatches.
# Stubs are provided so the module loads even without them.
try:
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain.agents.middleware.human_in_the_loop import (
        Action, ActionRequest, ApproveDecision, Decision, DecisionType,
        EditDecision, HITLRequest, HITLResponse, InterruptOnConfig,
        RejectDecision, ReviewConfig,
    )
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.runtime import Runtime
    from langgraph.types import interrupt as _lg_interrupt
    from typing_extensions import override

    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    AIMessage = None
    ToolMessage = None
    ToolCallRequest = None
    Runtime = None
    _lg_interrupt = None
    AgentMiddleware = object
    AgentState = dict
    Action = None
    ActionRequest = None
    ApproveDecision = None
    Decision = None
    DecisionType = None
    EditDecision = None
    HITLRequest = None
    HITLResponse = None
    InterruptOnConfig = None
    RejectDecision = None
    ReviewConfig = None

    def override(f):
        return f

    class _AgentMiddlewareStub:
        pass

    AgentMiddleware = _AgentMiddlewareStub


def interrupt(value):
    """Wrapper for langgraph interrupt, gracefully degrades."""
    if _lg_interrupt is not None:
        return _lg_interrupt(value)
    raise RuntimeError("langgraph interrupt() not available — no human-in-the-loop transport")


@dataclass
class HITLConfig:
    mode: ApprovalMode = ApprovalMode.SMART
    timeout: int = 60
    deny_rules: list[str] = field(default_factory=list)
    yolo_mode: bool = False
    write_approval_memory: bool = False
    write_approval_skills: bool = False
    clarify_timeout: int = 3600
    kanban_recurrence_limit: int = BLOCK_RECURRENCE_LIMIT
    mcp_reload_confirm: bool = True
    destructive_slash_confirm: bool = True
    smart_approval_llm: Any | None = None
    interrupted_tools: dict[str, bool | "InterruptOnConfig"] = field(default_factory=dict)
    description_prefix: str = "Action requires human approval"
