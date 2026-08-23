"""Base definitions and registration/firing mechanism for sub-agent lifecycle hooks."""

from pydantic import BaseModel
from typing import Callable, Awaitable


class SubagentStartEvent(BaseModel):
    """Event payload fired when a sub-agent starts."""
    parent_session_key: str
    child_session_key: str
    child_role: str
    child_goal: str


class SubagentStopEvent(BaseModel):
    """Event payload fired when a sub-agent stops."""
    parent_session_key: str
    child_session_key: str
    child_role: str
    child_status: str
    child_summary: str | None = None
    duration_ms: float | None = None


# Hook callback type definition
HookCallback = Callable[[SubagentStartEvent | SubagentStopEvent], Awaitable[None]]

# Registered hook lists
_start_hooks: list[HookCallback] = []
_stop_hooks: list[HookCallback] = []


def register_start_hook(callback: HookCallback) -> None:
    """Register a callback to be fired when a sub-agent starts."""
    _start_hooks.append(callback)


def register_stop_hook(callback: HookCallback) -> None:
    """Register a callback to be fired when a sub-agent stops."""
    _stop_hooks.append(callback)


async def fire_start_hooks(event: SubagentStartEvent) -> None:
    """Fire all start hooks; individual hook errors are silently swallowed."""
    for hook in _start_hooks:
        try:
            await hook(event)
        except Exception:
            pass


async def fire_stop_hooks(event: SubagentStopEvent) -> None:
    """Fire all stop hooks; individual hook errors are silently swallowed."""
    for hook in _stop_hooks:
        try:
            await hook(event)
        except Exception:
            pass


def clear_hooks() -> None:
    """Remove all registered start and stop hooks."""
    _start_hooks.clear()
    _stop_hooks.clear()
