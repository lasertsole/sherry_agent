"""Event bus for future_subagent internal messaging.

Provides a lightweight InboundMessage dataclass and a shared EventBus
(async Queue) that decouples subagent result delivery, session cleanup,
and A2A communication from the project's global MessageBus.
"""

from .core import InboundMessage, EventBus, get_event_bus

__all__ = [
    "InboundMessage",
    "EventBus",
    "get_event_bus",
]
