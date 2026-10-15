"""TaskFlow store timeouts, persist retries, wait_all polling, and the flow resource ceilings."""

from typing import TypedDict


class TaskFlowInfraConfig(TypedDict):
    """TaskFlow store timeouts, persist retries, wait_all polling, and the flow resource ceilings."""

    busy_timeout_ms: int
    init_wait_timeout_s: float
    persist_max_attempts: int
    wait_all_min_poll_interval_seconds: float
    wait_all_default_timeout_seconds: float
    wait_all_default_poll_interval_seconds: float
    waiting_timeout_hours: int
    #: Ceiling on a flow's step count, enforced by ``taskflow_create`` (refuse,
    #: never truncate). ``taskflow_plan``'s planner has its own, smaller
    #: ``PLAN_MAX_STEPS`` clamp before it ever reaches the store.
    max_steps_per_flow: int
    #: Ceiling on a session's ACTIVE flows (running/waiting), enforced by
    #: ``taskflow_create`` and ``taskflow_plan``. Finished / cancelled / failed
    #: flows do not count — they no longer hold a lane of planning.
    max_flows_per_session: int


TASKFLOW_INFRA: TaskFlowInfraConfig = {
    "busy_timeout_ms": 5000,
    "init_wait_timeout_s": 10.0,
    "persist_max_attempts": 3,
    "wait_all_min_poll_interval_seconds": 0.05,
    "wait_all_default_timeout_seconds": 300.0,
    "wait_all_default_poll_interval_seconds": 0.5,
    "waiting_timeout_hours": 24,
    # Generous next to the planner's 3-8 step guidance: this refuses a
    # hand-authored avalanche, it does not shape normal flows.
    "max_steps_per_flow": 20,
    # A long job may legitimately open a handful of concurrent flows; 20 is well
    # past that, and a session that reaches it is accumulating plans instead of
    # closing them.
    "max_flows_per_session": 20,
}
