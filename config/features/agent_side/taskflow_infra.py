"""TaskFlow store timeouts, persist retries, and wait_all polling."""

from typing import TypedDict


class TaskFlowInfraConfig(TypedDict):
    """TaskFlow store timeouts, persist retries, and wait_all polling."""

    busy_timeout_ms: int
    init_wait_timeout_s: float
    persist_max_attempts: int
    wait_all_min_poll_interval_seconds: float
    wait_all_default_timeout_seconds: float
    wait_all_default_poll_interval_seconds: float


TASKFLOW_INFRA: TaskFlowInfraConfig = {
    "busy_timeout_ms": 5000,
    "init_wait_timeout_s": 10.0,
    "persist_max_attempts": 3,
    "wait_all_min_poll_interval_seconds": 0.05,
    "wait_all_default_timeout_seconds": 300.0,
    "wait_all_default_poll_interval_seconds": 0.5,
}
