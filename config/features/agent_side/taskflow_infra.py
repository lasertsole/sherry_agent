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
    #: A RUNNING/WAITING flow whose ``updated_at`` is older than this is failed
    #: by the liveness sweep (inactivity timeout).
    stale_action_hours: int
    #: Grace before a flow unreferenced by any active todo is collected by the
    #: tri-color sweep. The flow and its auto-created todo live in separate
    #: SQLite files, so this window covers the create/bind race.
    tri_color_grace_seconds: float
    #: Finish-gate budgets. They are INDEPENDENT: a nudge does not consume the
    #: failure budget, and a protocol violation has its own streak.
    finish_nudge_max: int
    finish_failure_limit: int
    finish_protocol_violation_limit: int
    #: Per-session total flow rows (active + terminal + superseded). The prune
    #: deletes the oldest terminal/superseded rows when the cap is exceeded.
    #: 0 disables; ``prune_debounce_seconds`` throttles the prune.
    max_total_flows_per_session: int
    prune_debounce_seconds: float
    #: Ceiling on a flow's step count, enforced by ``taskflow_create`` (refuse,
    #: never truncate). ``taskflow_plan``'s planner has its own, smaller
    #: ``PLAN_MAX_STEPS`` clamp before it ever reaches the store.
    max_steps_per_flow: int
    #: Ceiling on a session's ACTIVE flows (running/waiting), enforced by
    #: ``taskflow_create`` and ``taskflow_plan``. Finished / cancelled / failed
    #: flows do not count — they no longer hold a lane of planning.
    max_flows_per_session: int
    #: How many step children ONE dispatch call may spawn at the same time. The
    #: SUBAGENT lane is the global backpressure (over-limit children queue as
    #: PENDING); this cap keeps a single dispatch wave from flooding it. 1
    #: restores the historical one-at-a-time behaviour.
    max_parallel_dispatch: int


TASKFLOW_INFRA: TaskFlowInfraConfig = {
    "busy_timeout_ms": 5000,
    "init_wait_timeout_s": 10.0,
    "persist_max_attempts": 3,
    "wait_all_min_poll_interval_seconds": 0.05,
    "wait_all_default_timeout_seconds": 300.0,
    "wait_all_default_poll_interval_seconds": 0.5,
    "waiting_timeout_hours": 24,
    "stale_action_hours": 12,
    "tri_color_grace_seconds": 300.0,
    "finish_nudge_max": 2,
    "finish_failure_limit": 2,
    "finish_protocol_violation_limit": 3,
    "max_total_flows_per_session": 200,
    "prune_debounce_seconds": 60.0,
    # Generous next to the planner's 3-8 step guidance: this refuses a
    # hand-authored avalanche, it does not shape normal flows.
    "max_steps_per_flow": 20,
    # A long job may legitimately open a handful of concurrent flows; 20 is well
    # past that, and a session that reaches it is accumulating plans instead of
    # closing them.
    "max_flows_per_session": 20,
    # A wave spawns as one asyncio.gather, bounded here. Two is the graduated
    # default (the lane cap is 8): a wave of N steps costs ceil(N / this) spawn
    # rounds instead of N, and a slow spawn no longer serializes the wave.
    "max_parallel_dispatch": 2,
}
