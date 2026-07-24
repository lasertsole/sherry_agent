"""Channel hook mechanism for sub-agent lifecycle events."""

from .base import (
    SubagentStartEvent,
    SubagentStopEvent,
    register_start_hook,
    register_stop_hook,
    fire_start_hooks,
    fire_stop_hooks,
    clear_hooks,
)
from .progress import (
    register_spawned_hook,
    register_progress_hook,
    register_ended_hook,
    register_delivery_target_hook,
    clear_all_hooks,
    fire_spawned_hook,
    fire_progress_hook,
    fire_ended_hook,
    fire_delivery_target_hook,
)

__all__ = [
    "SubagentStartEvent",
    "SubagentStopEvent",
    "register_start_hook",
    "register_stop_hook",
    "fire_start_hooks",
    "fire_stop_hooks",
    "clear_hooks",
    "register_spawned_hook",
    "register_progress_hook",
    "register_ended_hook",
    "register_delivery_target_hook",
    "clear_all_hooks",
    "fire_spawned_hook",
    "fire_progress_hook",
    "fire_ended_hook",
    "fire_delivery_target_hook",
]
