"""HITL shared types, enums, constants, and configuration.

Defines the core data model for the Human-in-the-Loop middleware:
- :class:`ApprovalMode` / :class:`ApprovalDecision` — approval strategy & result
- :class:`ApprovalResult` — pipeline output with ``blocked`` convenience property
- :class:`WriteTarget` / :class:`TriageStatus` / :class:`SmartApprovalResult` — gate enums
- :class:`HITLConfig` — single-source-of-truth configuration dataclass
- LangChain/LangGraph type stubs — graceful degradation when imports fail
- :func:`interrupt` — thin wrapper around ``langgraph.types.interrupt``
"""

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
    """Approval strategy for the HITL pipeline.

    - ``SMART``: Use an LLM to auto-approve/deny before escalating to human.
    - ``MANUAL``: Always escalate to human for dangerous commands.
    """

    SMART = "smart"
    MANUAL = "manual"


class ApprovalDecision(str, Enum):
    """Decision type returned after a command is approved or denied.

    - ``ONCE``: Single-use approval — next invocation re-evaluates.
    - ``SESSION``: Approved for the remainder of the session (stored in state).
    - ``ALWAYS``: Added to permanent allowlist (persistent across sessions).
    - ``DENY``: Rejected — the command must not be retried.
    """

    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"


@dataclass
class ApprovalResult:
    """Result of a command/tool approval check.

    Attributes:
        approved: Whether the action is allowed to proceed.
        decision: The specific :class:`ApprovalDecision` if one was made.
        reason: Human-readable explanation of the result.
        pattern_key: Matched pattern identifier (for allowlist tracking).
    """

    approved: bool
    decision: ApprovalDecision | None = None
    reason: str = ""
    pattern_key: str = ""

    @property
    def blocked(self) -> bool:
        """Convenience: ``True`` when ``approved`` is ``False``."""
        return not self.approved


class WriteTarget(str, Enum):
    """Target for write-approval staging.

    - ``MEMORY``: Memory write (user profile / long-term storage).
    - ``SKILLS``: Skill definition write.
    """

    MEMORY = "memory"
    SKILLS = "skills"


class TriageStatus(str, Enum):
    """Status for the Kanban triage workflow.

    - ``TODO`` / ``IN_PROGRESS`` / ``BLOCKED`` / ``DONE``: Standard task lifecycle.
    - ``TRIAGE``: Escalated to a human decision-maker after exceeding recurrence limit.
    """

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    TRIAGE = "triage"
    DONE = "done"


BLOCK_RECURRENCE_LIMIT = 3


class SmartApprovalResult(str, Enum):
    """Result of a smart-approval LLM assessment.

    - ``APPROVE``: LLM deemed the command safe.
    - ``DENY``: LLM deemed the command dangerous.
    - ``ESCALATE``: LLM uncertain — escalate to human.
    """

    APPROVE = "approve"
    DENY = "deny"
    ESCALATE = "escalate"


# Lazy langchain/langgraph imports — may fail due to version mismatches.
# Stubs are provided so the module loads even without them.
try:
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain.agents.middleware.human_in_the_loop import (
        Action,
        ActionRequest,
        ApproveDecision,
        Decision,
        DecisionType,
        EditDecision,
        HITLRequest,
        HITLResponse,
        InterruptOnConfig,
        RejectDecision,
        ReviewConfig,
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
    """Wrapper for ``langgraph.types.interrupt``, gracefully degrades.

    Args:
        value: The :class:`HITLRequest` to send to the human.

    Returns:
        The :class:`HITLResponse` from the human.

    Raises:
        RuntimeError: If ``langgraph`` is not installed or ``interrupt`` is unavailable.
    """
    if _lg_interrupt is not None:
        return _lg_interrupt(value)
    raise RuntimeError("langgraph interrupt() not available — no human-in-the-loop transport")


@dataclass
class HITLConfig:
    """Single-source-of-truth configuration for the Human-in-the-Loop middleware.

    Attributes:
        mode: Approval strategy (smart / manual).
        timeout: Default timeout in seconds for approval interrupts.
        deny_rules: Glob-style patterns for unconditional denial.
        write_approval_memory: Stage memory writes for human approval.
        write_approval_skills: Stage skill writes for human approval.
        clarify_timeout: Timeout for clarify() interrupts.
        kanban_recurrence_limit: Failure count before escalation to triage.
        mcp_reload_confirm: Require human consent for MCP server reload.
        destructive_slash_confirm: Require confirmation for destructive slash commands.
        smart_approval_llm: Optional LLM instance for smart command assessment.
        interrupted_tools: Mapping of tool_name → config for interrupt-on-use.
        description_prefix: Prefix for auto-generated interrupt descriptions.
    """

    mode: ApprovalMode = ApprovalMode.SMART
    timeout: int = 60
    deny_rules: list[str] = field(default_factory=list)
    write_approval_memory: bool = False
    write_approval_skills: bool = False
    clarify_timeout: int = 3600
    kanban_recurrence_limit: int = BLOCK_RECURRENCE_LIMIT
    mcp_reload_confirm: bool = True
    destructive_slash_confirm: bool = True
    smart_approval_llm: Any | None = None
    interrupted_tools: dict[str, bool | "InterruptOnConfig"] = field(default_factory=dict)
    description_prefix: str = "Action requires human approval"
