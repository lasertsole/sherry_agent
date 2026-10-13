"""Reminder catalogue: the dynamic ``<system-reminder>`` injection layer.

Importing this package registers the default sources (``plan_mode_block``,
``todo_block``, ``taskflow_status``, ``boulder_status``, ``continuity_context``,
``project_dir_notice``, ``knowledge_index``) and exposes the middleware that
injects them. ``TaskIntent`` / todo-continuation / workspace-notice keep their
own injection paths — the catalogue is an overlay, and a middleware that never
registers keeps working exactly as before.
"""

from .core import (
    ReminderChannel as ReminderChannel,
    ReminderSource as ReminderSource,
    clear_session as clear_session,
    register as register,
    registered_sources as registered_sources,
    resolve_injections as resolve_injections,
)
from .dynamic import (
    DynamicReminderMiddleware as DynamicReminderMiddleware,
    reset_session as reset_session,
)
from .sources import register_default_sources as _register_default_sources

_register_default_sources()

__all__ = [
    "DynamicReminderMiddleware",
    "ReminderChannel",
    "ReminderSource",
    "clear_session",
    "register",
    "registered_sources",
    "reset_session",
    "resolve_injections",
]
