"""Per-session spawn budget: how many children one session may ever start.

The concurrent-child cap (5) bounds how many children run AT A TIME; it does not
bound "finish a child → start the next" repeating forever, which is the shape a
stuck orchestrator loops on. This counter is the lifetime ceiling.

In-memory and process-local, like the rest of the registry's live view: retained
records are evicted past ``registry_max_retained_runs`` and terminal rows are
pruned, so no durable count is available. A count is dropped when its session is
purged (``server/service/session_cleanup_service.py``) and, like every other
in-memory counter here, resets with the process.
"""

import threading

# threading.Lock for the same reason as ``memory.py``: the registry is touched
# from the event loop AND from sweeper/persist threads, and the critical
# sections are sync and never await.
_lock = threading.Lock()
#: requester session key → accepted spawns so far (this process).
_spawn_counts: dict[str, int] = {}


def increment_spawn_count(requester_session_key: str) -> int:
    """Count one ACCEPTED spawn and return the new total.

    Called from ``register_run`` (a run that got that far is a real spawn, even
    if the child later fails) — not from the admission check, which also runs
    for attempts that never register.
    """
    with _lock:
        _spawn_counts[requester_session_key] = _spawn_counts.get(requester_session_key, 0) + 1
        return _spawn_counts[requester_session_key]


def total_spawns(requester_session_key: str) -> int:
    """Spawns this session has started in this process (0 when unknown)."""
    with _lock:
        return _spawn_counts.get(requester_session_key, 0)


def reset_spawn_count(requester_session_key: str) -> None:
    """Drop a session's count (session purge)."""
    with _lock:
        _spawn_counts.pop(requester_session_key, None)


def clear_all() -> None:
    """Drop every count (the in-memory run registry was cleared)."""
    with _lock:
        _spawn_counts.clear()
