"""Current-context endpoints: the toolbar's usage ring, and the context viewer.

Endpoints:
    GET /context_usage?session_id=<sid>
        -> {"success": true, "window": int, "total": int, "system": int,
            "tools": int, "messages": int}

        window    context window of the configured main LLM (MAIN_LLM_MAX_TOKEN)
        total     prompt size the provider reported for the session's last turn
        system    system-prompt estimate
        tools     main tool-schema estimate
        messages  the rest of the reported prompt (never negative)

    GET /context/inspect?session_id=<sid>
        -> the CONTENT behind those numbers, split the way the viewer shows it:
           ``system_prompt``, the enabled ``tools`` definitions, and the live
           ``messages`` from the checkpointer, each with its token estimate.

        400 when ``session_id`` is missing.
"""

from server.service.context_usage_service import get_context_content, get_context_usage
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


@app.get("/context/inspect")
async def context_inspect_handler(request):
    """Return the session's live context content (prompt, tools, transcript)."""
    session_id = request.query_params.get("session_id", "") or ""
    try:
        return ok(await get_context_content(session_id))
    except ValueError as error:
        return bad_request(str(error))
