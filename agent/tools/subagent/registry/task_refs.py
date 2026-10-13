"""Background asyncio.Task reference store for sub-agents, used to cancel running tasks during kill/steer."""

import asyncio
from loguru import logger

_task_refs: dict[str, asyncio.Task] = {}


def register_task(run_id: str, task: asyncio.Task) -> None:
    """Register a background asyncio.Task reference for a sub-agent run."""
    _task_refs[run_id] = task


def get_task(run_id: str) -> asyncio.Task | None:
    """Return the asyncio.Task reference for a run, or None."""
    return _task_refs.get(run_id)


def remove_task(run_id: str) -> asyncio.Task | None:
    """Remove and return the asyncio.Task reference for a run."""
    return _task_refs.pop(run_id, None)


def cancel_task(run_id: str) -> bool:
    """Cancel the asyncio.Task for a run. Returns True if cancellation was issued."""
    task = _task_refs.get(run_id)
    if task is None:
        return False
    if task.done():
        return False
    task.cancel()
    logger.info("Cancelled asyncio.Task for run {}", run_id)
    return True
