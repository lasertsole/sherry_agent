"""Current-context accounting endpoint for the chat toolbar's usage ring.

Endpoint:
    GET /context_usage?session_id=<sid>
        -> {"success": true, "window": int, "total": int, "system": int,
            "tools": int, "messages": int}

        window    context window of the configured main LLM (MAIN_LLM_MAX_TOKEN)
        total     prompt size the provider reported for the session's last turn
        system    system-prompt estimate
        tools     main tool-schema estimate
        messages  the rest of the reported prompt (never negative)

        400 when ``session_id`` is missing.
"""

from server.service.context_usage_service import get_context_usage
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok


@app.get("/context_usage")
async def context_usage_handler(request):
    """Return the session's context accounting (window, total, parts)."""
    session_id = request.query_params.get("session_id", "") or ""
    try:
        return ok(get_context_usage(session_id))
    except ValueError as error:
        return bad_request(str(error))
