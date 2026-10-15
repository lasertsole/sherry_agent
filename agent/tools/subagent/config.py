"""Subagent global configuration (singleton); defaults can be overridden via set_config()."""

import threading
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

MAX_SPAWN_DEPTH_CAP: int = 2


class SubagentConfig(BaseModel):
    """Global configuration for the subagent subsystem, covering spawn limits, timeouts, delivery tuning, and attachments."""

    model_config = ConfigDict(validate_assignment=True)

    max_spawn_depth: int = 2
    # Deprecated gate: spawn no longer rejects at this count — the SUBAGENT lane
    # queues over-limit runs (PENDING). Readable for backward compatibility only.
    max_concurrent: int = 8
    max_children_per_agent: int = 5
    # Lifetime spawn budget for one requesting session (process-lifetime, like
    # the rest of the registry's live view). The concurrency cap is 5 AT A TIME;
    # this bounds "finish a child, start the next" repeating forever.
    max_total_spawns_per_session: int = 50
    # Wall-clock ceiling for one child run (seconds). 0 = no timeout; the
    # default is a real ceiling because the goal loop (5 turns x 60 iterations)
    # is otherwise unbounded in TIME — a slow provider could run one child for
    # hours. Callers override per spawn via sessions_spawn(run_timeout_seconds).
    run_timeout_seconds: float = 600.0
    # Bounds the LLM tool (sessions_spawn) clamps a model-provided timeout to:
    # 10 min floor / 2 h ceiling. Internal callers (TaskFlow dispatch,
    # delegate_task) bypass the clamp and are trusted to pass sensible values.
    run_timeout_min_seconds: float = 600.0
    run_timeout_max_seconds: float = 7200.0
    require_agent_id: bool = False
    allow_agents: list[str] = Field(default_factory=lambda: ["*"])
    default_cleanup: Literal["delete", "keep"] = "delete"
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
    archive_after_minutes: int = 60  # 1 hour
    lifecycle_grace_period_seconds: float = 15.0

    attachments_enabled: bool = True
    attachments_max_files: int = 50
    attachments_max_file_bytes: int = 1 * 1024 * 1024  # 1 MB per file
    attachments_max_total_bytes: int = 5 * 1024 * 1024  # 5 MB total

    default_functional_role: str = "general"
    # Built-in role definitions live in-package (agent/tools/subagent/roles/definitions/);
    # this names the OPTIONAL untracked workspace override directory.
    roles_override_dir_name: str = "subagent_roles"

    @field_validator("max_spawn_depth")
    @classmethod
    def _enforce_spawn_depth_cap(cls, v: int) -> int:
        if v > MAX_SPAWN_DEPTH_CAP:
            raise ValueError(f"max_spawn_depth cannot exceed {MAX_SPAWN_DEPTH_CAP}")
        return v


_instance: SubagentConfig | None = None
#: Guards the first construction (see get_config).
_INSTANCE_LOCK = threading.Lock()


def get_config() -> SubagentConfig:
    """Return the global SubagentConfig singleton, creating it on first access.

    Double-checked under a module lock (two threads racing the first access must
    share one config, not one each).
    """
    global _instance
    if _instance is None:
        with _INSTANCE_LOCK:
            if _instance is None:
                _instance = SubagentConfig()
    return _instance


def set_config(config: SubagentConfig) -> None:
    """Replace the global SubagentConfig singleton with a custom instance."""
    global _instance
    _instance = config
