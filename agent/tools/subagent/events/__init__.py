"""Event bus for future_subagent internal messaging.

Provides a lightweight InboundMessage dataclass and a shared EventBus
(async Queue) that decouples subagent result delivery, session cleanup,
and A2A communication from the project's global MessageBus.

The bridge module connects EventBus → global MessageBus so that
sub-agent announcements reach the existing channel/websocket pipeline.
"""

from .core import InboundMessage, EventBus, get_event_bus
from .bridge import start_bridge, stop_bridge, is_bridge_running

__all__ = [
    "InboundMessage",
    "EventBus",
    "get_event_bus",
    "start_bridge",
    "stop_bridge",
    "is_bridge_running",
]
