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
