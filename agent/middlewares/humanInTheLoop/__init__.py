"""Human-In-The-Loop middleware for the hermes-agent.

Exports all public components — types, detectors, approval pipeline,
gates, and the main :class:`humanInTheLoop` middleware class.

Typical usage::

    from agent.middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig

    middleware = HumanInTheLoop(HITLConfig(mode=ApprovalMode.SMART))
"""

from .types import (
    ApprovalMode,
    ApprovalDecision,
    ApprovalResult,
    SmartApprovalResult,
    WriteTarget,
    TriageStatus,
    BLOCK_RECURRENCE_LIMIT,
    HITLConfig,
    BLOCKED_MESSAGE,
    LANGCHAIN_AVAILABLE,
)
from .detection import (
    detect_hardline_command,
    detect_dangerous_command,
    detect_clawhub_command,
    HARDLINE_PATTERNS,
    DANGEROUS_PATTERNS,
    CLAWHUB_REMOTE_NPM_PATTERNS,
    CLAWHUB_REMOTE_NPM_TAG,
)
from .approval import ApprovalPipeline, _extract_pattern, _args_hash
from .approval_scope import (
    NO_OPERATOR_MESSAGE,
    current_operator,
    operator_scope,
    resolve_turn_operator,
    set_operator,
)
from .approval_store import (
    ApprovalEvaluation,
    ApprovalVerdict,
    ToolApprovalStore,
)
from .gates import (
    PendingWrite,
    WriteApprovalGate,
    InterruptManager,
    MCPElicitationConsent,
    KanbanTriage,
    PairingStore,
    SlashConfirm,
)
from .core import HumanInTheLoop

__all__ = [
    "HumanInTheLoop",
    "HITLConfig",
    "ApprovalMode",
    "ApprovalDecision",
    "ApprovalResult",
    "SmartApprovalResult",
    "WriteTarget",
    "PendingWrite",
    "TriageStatus",
    "BLOCK_RECURRENCE_LIMIT",
    "BLOCKED_MESSAGE",
    "LANGCHAIN_AVAILABLE",
    "detect_hardline_command",
    "detect_dangerous_command",
    "detect_clawhub_command",
    "HARDLINE_PATTERNS",
    "DANGEROUS_PATTERNS",
    "CLAWHUB_REMOTE_NPM_PATTERNS",
    "CLAWHUB_REMOTE_NPM_TAG",
    "ApprovalPipeline",
    "_extract_pattern",
    "_args_hash",
    "ApprovalEvaluation",
    "ApprovalVerdict",
    "ToolApprovalStore",
    "NO_OPERATOR_MESSAGE",
    "current_operator",
    "operator_scope",
    "resolve_turn_operator",
    "set_operator",
    "WriteApprovalGate",
    "InterruptManager",
    "MCPElicitationConsent",
    "KanbanTriage",
    "PairingStore",
    "SlashConfirm",
]
