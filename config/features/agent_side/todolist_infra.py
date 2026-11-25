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
    #: todo: ``refuse`` rejects the replacement (the default — a full-list
    #: rewrite that forgets the flow root is almost always a model slip, and
    #: cancelling destroys in-flight step children; measured: one replacement
    #: killed a 5-step flow with two children mid-flight and cascaded every step
    #: to failed), ``cancel`` immediately fails the orphaned flow, ``sweep_only``
    #: leaves it to the background sweep.
    orphan_flow_on_drop: str
    #: Run a session-scoped unreferenced-flow sweep immediately after a full
    #: replacement (defense in depth behind ``orphan_flow_on_drop``).
    trigger_sweep_after_replace: bool
    stagnation_max_stagnation: int
    #: Consecutive continuation turns whose WORKING-TREE DIFF hash is unchanged
    #: (and no step child is in flight) before the gate stops. Complements
    #: ``stagnation_max_stagnation`` (which watches the plan TEXT): a model can
    #: keep editing todos while never changing the code — diff stagnation
    #: catches that shape.
    diff_stagnation_threshold: int
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
    #: Auto-insert exactly one system verify todo as the LAST step of every
    #: non-empty plan (MAIN agent only). There is no minimum work-item count:
    #: a one-item plan is exactly the case that used to skip the mandatory
    #: acceptance check altogether (measured on a DeepSWE task whose one-line
    #: plan ended the turn with the verify never inserted).
    verify_todo_enabled: bool
    #: How many reviewer links a verify todo may replace before the gate offers
    #: the exhausted escape (cancel + verify yourself).
    max_verify_respawns: int
    #: Max seconds to wait at verify completion for in-flight TaskFlow step
    #: children to settle (their edits may still be writing to disk after the
    #: step reports done). Bounded: a child that never settles gets a warning
    #: injected instead of blocking completion forever. Eval uses 180 s; the
    #: product keeps 30 s — children normally flush in a few seconds.
    verify_settle_wait_s: float


TODOLIST_INFRA: TodoListInfraConfig = {
    "store_busy_timeout_ms": 5000,
    "store_init_wait_timeout_s": 10.0,
    "position_shift_offset": 1_000_000,
    "orphan_flow_on_drop": "refuse",
    "trigger_sweep_after_replace": True,
    "stagnation_max_stagnation": 3,
    "diff_stagnation_threshold": 2,
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
    "verify_todo_enabled": True,
    "max_verify_respawns": 2,
    "verify_settle_wait_s": 30.0,
}
