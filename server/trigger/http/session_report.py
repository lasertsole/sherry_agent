"""GET /sessions/:session_id/report — the session run report (post-task analysis)."""

from __future__ import annotations

from server.service import session_report_service
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, to_text_response


async def _report_response(request, session_id: str) -> object:
    session_id = str(session_id or "").strip()
    if not session_id:
        return bad_request("session_id is required")
    refresh = request.query_params.get("refresh", "").lower() in {"1", "true", "yes"}
    try:
        report = await session_report_service.get_session_report(session_id, refresh=refresh)
    except Exception:  # noqa: BLE001 - analysis is best-effort
        return to_text_response(200, {"session_id": session_id, "error": "report unavailable"})
    return ok(report)


@app.get("/sessions/:session_id/report")
async def session_report_handler(request, path_params):
    return await _report_response(request, (path_params or {}).get("session_id") or "")


# Query-parameter form matches the client's fetchApiPayload convention
# (the context inspector uses the same /path?session_id= shape).
@app.get("/sessions/report")
async def session_report_query_handler(request):
    return await _report_response(request, request.query_params.get("session_id", ""))
