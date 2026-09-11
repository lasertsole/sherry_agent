"""Context-engine nudge thresholds and multimodal temp retention."""

from typing import TypedDict


class ContextEngineHookConfig(TypedDict):
    """Context-engine nudge thresholds and multimodal temp retention."""

    nudge_memory_threshold: int
    nudge_skill_threshold: int
    multimodal_temp_retention_days: int


CONTEXT_ENGINE_HOOK: ContextEngineHookConfig = {
    "nudge_memory_threshold": 10,
    "nudge_skill_threshold": 10,
    "multimodal_temp_retention_days": 7,
}
