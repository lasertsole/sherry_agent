"""TodoList store timeouts and stagnation-tracker thresholds."""

from typing import TypedDict


class TodoListInfraConfig(TypedDict):
    """TodoList store timeouts and stagnation-tracker thresholds."""

    store_busy_timeout_ms: int
    store_init_wait_timeout_s: float
    stagnation_max_stagnation: int
    stagnation_base_cooldown_s: float
    stagnation_max_cooldown_s: float
    stagnation_failure_reset_window_s: int
    stagnation_max_recovery_attempts: int
    #: How long a DEFERRED continuation waits for the ending turn to release its
    #: own busy signals (the enforcer's after_agent hook runs while the client's
    #: stream task is still live, so an immediate trigger would see "busy" and
    #: drop the directive). Reaching the cap means a real user turn took over.
    continuation_self_idle_wait_s: float
    #: Poll interval of that wait.
    continuation_self_idle_poll_s: float


TODOLIST_INFRA: TodoListInfraConfig = {
    "store_busy_timeout_ms": 5000,
    "store_init_wait_timeout_s": 10.0,
    "stagnation_max_stagnation": 3,
    "stagnation_base_cooldown_s": 2.0,
    "stagnation_max_cooldown_s": 60.0,
    "stagnation_failure_reset_window_s": 300,
    "stagnation_max_recovery_attempts": 2,
    "continuation_self_idle_wait_s": 30.0,
    "continuation_self_idle_poll_s": 0.5,
}
