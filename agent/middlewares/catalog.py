"""The main chain's middleware names, in registration order.

:data:`MIDDLEWARE_ORDER` documents the chain the way ``_build_middlewares``
registers it. Kept beside the chain rather than derived from it (constructing
``_build_middlewares`` would build real LLMs). ``DelegationPressureGate`` is
appended only when its feature flag is enabled.

Chain membership is a BUILD-TIME contract — a user cannot switch an entry off.
The per-session middleware selection was removed together with the preset
panel's middleware tab: a session config no longer carries a disabled list or
per-middleware options, and every registered entry runs.
"""

from __future__ import annotations

__all__ = ["MIDDLEWARE_ORDER"]

#: Registration order of the main-agent chain (``agent/core.py::_build_middlewares``).
MIDDLEWARE_ORDER: tuple[str, ...] = (
    "PlanContinuationEnforcer",
    "system_prompt_injection",
    "WorkspaceNoticeMiddleware",
    "PlanModeToolGate",
    "MultimodalProcessor",
    "IterationBudget",
    "TokenBudget",
    "ToolResultEnrichmentMiddleware",
    "ToolGuardrails",
    "ToolResourceLockMiddleware",
    "UserHooksMiddleware",
    "ContextEvictionMiddleware",
    "ToolCallNormalize",
    "PathGuard",
    "SubagentCompletionDrainMiddleware",
    "TaskIntentMiddleware",
    "DynamicReminderMiddleware",
    "InputSafetyFilter",
    "LLMModerationMiddleware",
    "OutputRepetitionGuard",
    "MaxTokensBoostMiddleware",
    "ThinkingControlMiddleware",
    "HeartbeatStaleness",
    "HumanInTheLoop",
    "MessagePersistenceMiddleware",
    "LLMRetryMiddleware",
    "OutputSafetyFilter",
    "Summarization",
    "InvalidToolCallRepairMiddleware",
    "DelegationPressureGate",
)
