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
    # reliable. The channels/subagent imports below start background threads
    # via their import side-effect chain (channel manager, subagent
    # consumers) and must NOT run while the crash-loop breaker has tripped;
    # ws/http imports stay unconditional so REST + WS remain available.
    if os.environ.get("SHERRY_HTTP_ONLY") != "1":
        import server.trigger.channels  # noqa: F401  (side-effect route registration)
        import server.trigger.subagent  # noqa: F401  (side-effect route registration)
