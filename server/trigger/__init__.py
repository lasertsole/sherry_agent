from .http import app

# Let top-level code run (imports needed for side-effect registration)
import server.trigger.channels
import server.trigger.subagent

__all__ = ["app"]