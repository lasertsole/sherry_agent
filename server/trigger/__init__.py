import os

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

    # Crash-loop HTTP-only mode: ``server.__main__`` sets SHERRY_HTTP_ONLY=1
    # BEFORE importing this package (timing is safe), so the env check here is
    # reliable. The channel event-loop thread and the subagent registry
    # scheduling below must NOT run while the crash-loop breaker has tripped;
    # ws/http registration stays unconditional so REST + WS remain available.
    # The imports only bind registration seams — the background work is the
    # explicit ``start()`` calls, invoked here at the assembly point.
    if os.environ.get("SHERRY_HTTP_ONLY") != "1":
        from server.trigger import channels as _channels
        from server.trigger import subagent as _subagent

        _channels.start()
        _subagent.start()
