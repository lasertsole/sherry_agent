"""Subagent global configuration (singleton); defaults can be overridden via set_config()."""

from pydantic import BaseModel, Field
from typing import Literal
from .types.spawn import ContextMode


class SubagentConfig(BaseModel):
    """Global configuration for the future_subagent subsystem, covering spawn limits, timeouts, delivery tuning, and attachments."""
    max_spawn_depth: int = 3
    max_children_per_agent: int = 5
    run_timeout_seconds: float = 300.0
    require_agent_id: bool = False
    allow_agents: list[str] = Field(default_factory=lambda: ["*"])
    default_cleanup: Literal["delete", "keep"] = "delete"
    default_context_mode: ContextMode = ContextMode.ISOLATED
    announce_retry_max: int = 3
    announce_retry_delay_base_ms: int = 1000
    delivery_suspend_soft_cap: int = 25
    delivery_suspend_hard_cap: int = 50
    delivery_suspend_target: int = 10  # target active deliveries after suspension drains
    sweeper_interval_seconds: int = 60
    orphan_recovery_delay_seconds: int = 120

    announce_expiry_ms: int = 7_200_000  # 2 hours
    announce_hard_expiry_ms: int = 86_400_000  # 24 hours
    max_announce_retry_count: int = 10
    stale_unended_threshold_seconds: int = 7200  # 2 hours without an end signal
    recent_ended_window_seconds: int = 1800  # 30 min window to consider a run "recently ended"
    steer_rate_limit_ms: int = 2000
    archive_after_minutes: int = 1440  # 24 hours
    lifecycle_grace_period_seconds: float = 15.0

    attachments_enabled: bool = True
    attachments_max_files: int = 50
    attachments_max_file_bytes: int = 1 * 1024 * 1024  # 1 MB per file
    attachments_max_total_bytes: int = 5 * 1024 * 1024  # 5 MB total


_instance: SubagentConfig | None = None


def get_config() -> SubagentConfig:
    """Return the global SubagentConfig singleton, creating it on first access."""
    global _instance
    if _instance is None:
        _instance = SubagentConfig()
    return _instance


def set_config(config: SubagentConfig) -> None:
    """Replace the global SubagentConfig singleton with a custom instance."""
    global _instance
    _instance = config
