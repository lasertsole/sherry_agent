from .core import app

__all__ = ["app"]


def init() -> None:
    """Register all HTTP/WS/channel/subagent routes and handlers.

    Used to run at module import time (``import server.trigger`` pulled in
    every route module for side-effect registration), which made any bare
    import of this package register routes unexpectedly. Importing the
    package now only constructs the app object;
    the service entry point calls ``init()`` once.

    Idempotent: module caching makes the imports below no-ops on
    subsequent calls, so routes can never register twice.
    """
    import server.trigger.ws  # noqa: F401  (side-effect route registration)
    import server.trigger.http  # noqa: F401  (side-effect route registration)
    import server.trigger.channels  # noqa: F401  (side-effect route registration)
    import server.trigger.subagent  # noqa: F401  (side-effect route registration)
