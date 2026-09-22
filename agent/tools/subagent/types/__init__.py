"""Types sub-package: all data models and enum definitions for the subagent system."""

from .spawn import SpawnMode, ContextMode
from .registry import (
    ExecutionStatus,
    DeliveryStatus,
    RunOutcomeStatus,
    RunOutcome,
    ExecutionState,
    CompletionState,
    CompletionDeliveryState,
    SubagentRunRecord,
)
from .lifecycle import LifecycleEndedReason, LifecycleEndedOutcome
from .delivery import DeliveryContext
from .capability import SubagentSessionRole, ControlScope
from .functional_role import FunctionalRole
from .swarm import SwarmMode, SwarmRunState, SwarmGroupConfig

__all__ = [
    "SpawnMode",
    "ContextMode",
    "ExecutionStatus",
    "DeliveryStatus",
    "RunOutcomeStatus",
    "RunOutcome",
    "ExecutionState",
    "CompletionState",
    "CompletionDeliveryState",
    "SubagentRunRecord",
    "LifecycleEndedReason",
    "LifecycleEndedOutcome",
    "DeliveryContext",
    "SubagentSessionRole",
    "ControlScope",
    "FunctionalRole",
    "SwarmMode",
    "SwarmRunState",
    "SwarmGroupConfig",
]
