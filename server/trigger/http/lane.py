"""Lane status endpoint: per-lane concurrency counters for observability.

Endpoint:
    GET /lane-status
        -> {"main": {"name", "max_concurrent", "active", "queued"}, ...}
"""

from server.trigger.core import app


@app.get("/lane-status")
async def lane_status_handler(request):
    """Return the process-level lane snapshot for all four lanes."""
    from runtime.lane import get_lane_manager

    return get_lane_manager().snapshot()


@app.get("/sandbox/status")
async def sandbox_status_handler(request):
    """Report the effective sandbox policy + whether a backend is usable.

    Read-only observability for the write barrier the shell-ish tools rely on:
    ``available: false`` with ``policy: required`` means those tools will refuse
    to execute; with ``policy: auto`` it means they run unsandboxed.
    """
    from server.service.sandbox_health import sandbox_status

    return sandbox_status()
