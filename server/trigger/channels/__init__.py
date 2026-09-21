import server.trigger.channels.core  # noqa: F401  (side-effect route registration)
from server.trigger.channels.core import start

__all__ = ["start"]
