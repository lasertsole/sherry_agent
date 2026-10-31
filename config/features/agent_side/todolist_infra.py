"""TodoList store timeouts and stagnation-tracker thresholds."""

from typing import TypedDict


class TodoListInfraConfig(TypedDict):
    """TodoList store timeouts and stagnation-tracker thresholds."""

    store_busy_timeout_ms: int
    store_init_wait_timeout_s: float
    #: Temporary position offset used to make room for an insertion without a
    #: PRIMARY KEY (session_id, position) collision. A single session never
    #: reaches this many todos.
    position_shift_offset: int
    #: What a full ``todowrite`` replacement does when it drops a flow's GC-root
    #: todo: ``cancel`` immediately fails the orphaned flow, ``refuse`` rejects
    #: the replacement, ``sweep_only`` leaves it to the background sweep.
    orphan_flow_on_drop: str
    #: Run a session-scoped unreferenced-flow sweep immediately after a full
    #: replacement (defense in depth behind ``orphan_flow_on_drop``).
    trigger_sweep_after_replace: bool
    stagnation_max_stagnation: int
    stagnation_base_cooldown_s: float
    stagnation_max_cooldown_s: float
    stagnation_failure_reset_window_s: int
    stagnation_max_recovery_attempts: int
    #: Turns without a ``todowrite`` call after which a continuation
    #: directive carries the todo-tracking nudge (the model may be
    #: advancing TaskFlow while the TODO list goes stale).
    todo_write_nudge_turns: int
    #: How long a DEFERRED continuation waits for the ending turn to release its
    #: own busy signals (the enforcer's after_agent hook runs while the client's
    #: stream task is still live, so an immediate trigger would see "busy" and
    #: drop the directive). Reaching the cap means a real user turn took over.
    continuation_self_idle_wait_s: float
    #: Poll interval of that wait.
    continuation_self_idle_poll_s: float
    #: Consecutive CONTINUATION turns before the enforcer stops injecting.
    #: Counts the instructions ``mark_injected`` confirmed delivered (a failed
    #: trigger does not count); a real user message or a subagent-completion
    #: turn resets it. Hitting the cap injects one final "stop and summarize"
    #: directive and the session then stays silent until the user speaks.
    max_continuation_turns: int


TODOLIST_INFRA: TodoListInfraConfig = {
    "store_busy_timeout_ms": 5000,
    "store_init_wait_timeout_s": 10.0,
    "position_shift_offset": 1_000_000,
    "orphan_flow_on_drop": "cancel",
    "trigger_sweep_after_replace": True,
    "stagnation_max_stagnation": 3,
    "stagnation_base_cooldown_s": 2.0,
    "stagnation_max_cooldown_s": 60.0,
    "stagnation_failure_reset_window_s": 300,
    "stagnation_max_recovery_attempts": 2,
    "todo_write_nudge_turns": 10,
    "continuation_self_idle_wait_s": 30.0,
    "continuation_self_idle_poll_s": 0.5,
    # Stagnation stops a stuck plan in ~5 turns (3 + 2 recoveries); the cap is
    # 3x that, so a plan that KEEPS MAKING PROGRESS still has room — the shape
    # stagnation cannot see is the "edits the plan every turn, never finishes"
    # loop this ceiling exists for.
    "max_continuation_turns": 15,
}
