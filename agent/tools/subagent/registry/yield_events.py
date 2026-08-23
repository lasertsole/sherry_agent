"""Yield pause/wake mechanism: parent agents yield and are woken when all children have settled."""

import asyncio
from loguru import logger
from ..types.registry import ExecutionStatus
from . import queries

_yield_events: dict[str, asyncio.Event] = {}


def register_yield_event(session_key: str) -> asyncio.Event:
    """Register an asyncio.Event for a session so the parent agent can await it for yield-pause."""
    event = asyncio.Event()
    _yield_events[session_key] = event
    logger.debug("Registered yield event for session {}", session_key)
    return event


def get_yield_event(session_key: str) -> asyncio.Event | None:
    """Return the yield Event for a session, or None if not registered."""
    return _yield_events.get(session_key)


def remove_yield_event(session_key: str) -> None:
    """Remove the yield Event for a session."""
    _yield_events.pop(session_key, None)


def wake_yield(session_key: str) -> bool:
    """Wake a yield-paused parent agent by setting its Event. Returns True if woken."""
    event = _yield_events.get(session_key)
    if event is None:
        return False
    if not event.is_set():
        event.set()
        logger.info("Woke yield-paused session {}", session_key)
    return True


async def wake_yield_if_all_children_settled(session_key: str) -> bool:
    """Wake the parent only if all child runs have ended (RUNNING/INTERRUPTED count is zero)."""
    from . import all_runs

    children = queries.list_runs_for_requester(session_key)
    active = [
        r for r in children
        if r.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)
    ]

    if active:
        return False

    return wake_yield(session_key)
