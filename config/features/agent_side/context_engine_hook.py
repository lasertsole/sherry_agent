"""Context-engine nudge thresholds, plan extraction, and multimodal temp retention."""

from typing import TypedDict


class ContextEngineHookConfig(TypedDict):
    """Context-engine nudge thresholds, plan extraction, and multimodal temp retention."""

    nudge_memory_threshold: int
    plan_extraction_enabled: bool
    multimodal_temp_retention_days: int


CONTEXT_ENGINE_HOOK: ContextEngineHookConfig = {
    "nudge_memory_threshold": 10,
    "plan_extraction_enabled": True,
    "multimodal_temp_retention_days": 7,
}
