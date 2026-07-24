"""Thread-safe in-memory store for SubagentRunRecord instances using a dict with lock-based access."""

import threading
from typing import Iterator
from ..types.registry import SubagentRunRecord

_lock = threading.Lock()
_runs: dict[str, SubagentRunRecord] = {}


def get(run_id: str) -> SubagentRunRecord | None:
    """Return the run record for the given ID, or None if not found."""
    with _lock:
        return _runs.get(run_id)


def set_run(run: SubagentRunRecord) -> None:
    """Insert or replace a run record keyed by run_id."""
    with _lock:
        _runs[run.run_id] = run


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
