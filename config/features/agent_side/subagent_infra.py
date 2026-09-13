"""Scattered subagent infrastructure tunables (outside SubagentConfig)."""

from typing import TypedDict


class SubagentInfraConfig(TypedDict):
    """Scattered subagent infrastructure tunables (outside SubagentConfig)."""

    registry_store_busy_timeout_ms: int
    registry_init_wait_timeout_s: float
    pending_injections_busy_timeout_ms: int
    sessions_yield_default_timeout_seconds: float
    sessions_send_timeout_seconds: float
    thread_binding_idle_timeout_ms: int
    thread_binding_max_age_ms: int
    delivery_mirror_max: int
    delivery_transient_retry_delays_ms: list[int]
    delivery_compaction_retry_delays_ms: list[int]
    orphan_wedged_age_seconds: int
    orphan_max_recovery_attempts: int
    orphan_max_terminal_finalize_attempts: int
    helpers_frozen_result_cap_bytes: int
    helpers_announce_retry_base_ms: int
    helpers_announce_retry_cap_ms: int
    capture_max_wait_ms: int
    capture_retry_interval_ms: int
    steer_abort_settle_timeout: float
    followup_interval_multiplier: int


SUBAGENT_INFRA: SubagentInfraConfig = {
    "registry_store_busy_timeout_ms": 5000,
    "registry_init_wait_timeout_s": 10.0,
    "pending_injections_busy_timeout_ms": 5000,
    "sessions_yield_default_timeout_seconds": 300.0,
    "sessions_send_timeout_seconds": 30.0,
    "thread_binding_idle_timeout_ms": 300000,
    "thread_binding_max_age_ms": 86400000,
    "delivery_mirror_max": 5000,
    "delivery_transient_retry_delays_ms": [5000, 10000, 20000],
    "delivery_compaction_retry_delays_ms": [1000, 2000, 4000, 8000],
    "orphan_wedged_age_seconds": 86400,
    "orphan_max_recovery_attempts": 3,
    "orphan_max_terminal_finalize_attempts": 3,
    "helpers_frozen_result_cap_bytes": 24000,
    "helpers_announce_retry_base_ms": 1000,
    "helpers_announce_retry_cap_ms": 8000,
    "capture_max_wait_ms": 5000,
    "capture_retry_interval_ms": 500,
    "steer_abort_settle_timeout": 5.0,
    "followup_interval_multiplier": 2,
}
