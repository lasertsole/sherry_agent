"""Thread-safe in-memory store for SubagentRunRecord instances using a dict with lock-based access.

The dict holds the *live* view of every run this process knows about, so
terminal records are evicted once the cache outgrows :data:`_MAX_RETAINED_RUNS`
(oldest first; the sweeper has already persisted them to SQLite, and readers
that miss fall back to the disk registry). Without the cap a long-lived process
grows the dict without bound: the dev database held ~7.8k runs.
"""

import threading
from collections import OrderedDict

from config.features import SUBAGENT_INFRA
from ..types.registry import SubagentRunRecord

# threading.Lock is the correct primitive here (not asyncio.Lock): the registry
# is touched from the event loop AND from sweeper/persist threads, and every
# critical section below is sync and never awaits — asyncio.Lock is loop-bound,
# not thread-safe, and unusable from these sync accessors.
_lock = threading.Lock()
#: Insertion-ordered so the eviction pass can drop the oldest terminal runs.
_runs: "OrderedDict[str, SubagentRunRecord]" = OrderedDict()
#: Upper bound on retained records (config-driven).
_MAX_RETAINED_RUNS: int = SUBAGENT_INFRA["registry_max_retained_runs"]


def _is_terminal(run: SubagentRunRecord) -> bool:
    """Whether the run reached a final state (safe to evict from memory)."""
    status = getattr(getattr(run, "execution", None), "status", None)
    return str(getattr(status, "value", status) or "").upper() in {
        "COMPLETED",
        "FAILED",
        "TIMEOUT",
        "CANCELLED",
        "STOPPED",
    }


def _evict_overflow() -> None:
    """Drop the oldest terminal records until the dict fits the cap (lock held)."""
    if len(_runs) <= _MAX_RETAINED_RUNS:
        return
    for run_id in list(_runs.keys()):
        if len(_runs) <= _MAX_RETAINED_RUNS:
            break
        if _is_terminal(_runs[run_id]):
            del _runs[run_id]


def get(run_id: str) -> SubagentRunRecord | None:
    """Return the run record for the given ID, or None if not found."""
    with _lock:
        return _runs.get(run_id)


def set_run(run: SubagentRunRecord) -> None:
    """Insert or replace a run record keyed by run_id."""
    with _lock:
        _runs[run.run_id] = run
        _runs.move_to_end(run.run_id)
        _evict_overflow()


def delete(run_id: str) -> SubagentRunRecord | None:
    """Remove and return a run record, or None if it did not exist."""
    with _lock:
        return _runs.pop(run_id, None)


def update(run_id: str, **kwargs) -> SubagentRunRecord | None:
    """Atomically update fields on an existing run record; returns the updated record or None."""
    with _lock:
        run = _runs.get(run_id)
        if run is None:
            return None
        updated = run.model_copy(update=kwargs)
        _runs[run_id] = updated
        return updated


def snapshot() -> dict[str, SubagentRunRecord]:
    """Return a shallow copy of all run records, suitable for persistence."""
    with _lock:
        return dict(_runs)


def values() -> list[SubagentRunRecord]:
    """Return a list of all run records."""
    with _lock:
        return list(_runs.values())


def size() -> int:
    """Return the number of stored run records."""
    with _lock:
        return len(_runs)


def clear() -> None:
    """Remove all run records from memory."""
    with _lock:
        _runs.clear()


def find_by_child_session_key(child_session_key: str) -> SubagentRunRecord | None:
    """Find a run record by child_session_key, used to trace back from child agent to run record."""
    with _lock:
        for run in _runs.values():
            if run.child_session_key == child_session_key:
                return run
    return None
