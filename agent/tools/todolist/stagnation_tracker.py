"""E3 stagnation tracker: stagnation detection, backoff cooldown, abort
detection, and recovery-mode accounting for the todo-continuation enforcer.

Modeled on omo ``todo-continuation-enforcer``:

- ``CONTINUATION_COOLDOWN_MS`` → ``_BASE_COOLDOWN_S``
- ``MAX_CONSECUTIVE_FAILURES`` / ``MAX_STAGNATION_COUNT`` → ``_MAX_STAGNATION``
- ``FAILURE_RESET_WINDOW_MS`` → ``_FAILURE_RESET_WINDOW_S``

All state is module-level and per-session. The monotonic clock is read through
``_now`` so tests can drive time deterministically (no real sleeps). The module
is intentionally pure: no I/O, no logging, no event-loop assumptions.
"""

import time

from config.features import TODOLIST_INFRA

# Bound to the feature registry (single source of truth); names preserved.
_MAX_STAGNATION = TODOLIST_INFRA["stagnation_max_stagnation"]
_BASE_COOLDOWN_S = TODOLIST_INFRA["stagnation_base_cooldown_s"]
_MAX_COOLDOWN_S = TODOLIST_INFRA["stagnation_max_cooldown_s"]
_FAILURE_RESET_WINDOW_S = TODOLIST_INFRA["stagnation_failure_reset_window_s"]
_MAX_RECOVERY_ATTEMPTS = TODOLIST_INFRA["stagnation_max_recovery_attempts"]

# per-session state
_stagnation_count: dict[str, int] = {}
_last_snapshot: dict[str, str] = {}  # session_id → "content=status|..."
_last_inject_time: dict[str, float] = {}  # session_id → last injection timestamp
_last_failure_time: dict[str, float] = {}  # session_id → last stagnation timestamp
_recovery_attempts: dict[str, int] = {}  # session_id → recovery attempts used


def _now() -> float:
    """Monotonic clock seam (monkeypatched in tests for determinism)."""
    return time.monotonic()


def _snapshot(todos: list[dict]) -> str:
    """Stable snapshot of the list: content + status per todo, in order."""
    return "|".join(f"{t['content']}={t['status']}" for t in todos)


def check_stagnation(session_id: str, todos: list[dict]) -> bool:
    """Return True when the snapshot has been unchanged ``_MAX_STAGNATION`` times.

    On an unchanged snapshot the streak increments and the failure time is
    refreshed. On a changed snapshot the streak is cleared only when the last
    recorded failure is older than ``_FAILURE_RESET_WINDOW_S``; otherwise it is
    retained (an oscillation is still progress-stall evidence).
    """
    snapshot = _snapshot(todos)
    if _last_snapshot.get(session_id) == snapshot:
        _stagnation_count[session_id] = _stagnation_count.get(session_id, 0) + 1
        _last_failure_time[session_id] = _now()
    else:
        last_fail = _last_failure_time.get(session_id)
        if last_fail and (_now() - last_fail) > _FAILURE_RESET_WINDOW_S:
            _stagnation_count[session_id] = 0
        else:
            _stagnation_count[session_id] = _stagnation_count.get(session_id, 0)
    _last_snapshot[session_id] = snapshot
    return _stagnation_count.get(session_id, 0) >= _MAX_STAGNATION


def is_in_cooldown(session_id: str) -> bool:
    """True while the exponential-backoff cooldown since the last injection holds.

    Cooldown = ``min(_BASE_COOLDOWN_S * 2^min(failures, 5), _MAX_COOLDOWN_S)``.
    """
    last = _last_inject_time.get(session_id)
    if last is None:
        return False
    failures = _stagnation_count.get(session_id, 0)
    cooldown = min(_BASE_COOLDOWN_S * (2 ** min(failures, 5)), _MAX_COOLDOWN_S)
    return (_now() - last) < cooldown


def mark_injected(session_id: str) -> None:
    """Record the current time as the last injection."""
    _last_inject_time[session_id] = _now()


def should_enter_recovery(session_id: str) -> bool:
    """True when the session is stagnant and recovery attempts remain."""
    if _stagnation_count.get(session_id, 0) >= _MAX_STAGNATION:
        return _recovery_attempts.get(session_id, 0) < _MAX_RECOVERY_ATTEMPTS
    return False


def enter_recovery(session_id: str) -> None:
    """Consume a recovery attempt and clear the streak it responds to."""
    _stagnation_count[session_id] = 0
    _recovery_attempts[session_id] = _recovery_attempts.get(session_id, 0) + 1


def reset(session_id: str) -> None:
    """Clear all tracker state for a session (user takeover / all todos done)."""
    _stagnation_count.pop(session_id, None)
    _last_snapshot.pop(session_id, None)
    _last_inject_time.pop(session_id, None)
    _last_failure_time.pop(session_id, None)
    _recovery_attempts.pop(session_id, None)


def is_abort_error(error: Exception) -> bool:
    """True when ``error`` is an abort-class failure (user cancel / timeout).

    An aborted turn must never trigger continuation: the user or the runtime
    deliberately stopped the work.
    """
    error_str = str(error).lower()
    abort_markers = [
        "abort",
        "cancelled",
        "interrupted by user",
        "operation cancelled",
        "timeout",
    ]
    return any(marker in error_str for marker in abort_markers)
