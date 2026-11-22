"""External push entry: POST /sessions/notice → session-wide system.notice.

For producers that hold the gateway token but no websocket (CI webhooks,
deploy scripts, plugins): the handler validates and fans the notice out over
the session push channel exactly like the agent-side
:func:`runtime.session.system_notice.push_system_notice`.
"""

from __future__ import annotations

from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body
from runtime.session.system_notice import NOTICE_LEVELS, push_system_notice


@app.post("/sessions/notice")
async def push_system_notice_handler(request):
    """Fan one external notice out to every live socket of the session."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = str(body.get("session_id") or "").strip()
    title = str(body.get("title") or "").strip()
    if not session_id:
        return bad_request("session_id is required")
    if not title:
        return bad_request("title is required")
    level = str(body.get("level") or "info").strip()
    if level not in NOTICE_LEVELS:
        return bad_request(f"level must be one of {', '.join(NOTICE_LEVELS)}")
    content = await push_system_notice(
        session_id,
        title=title,
        content=str(body.get("content") or ""),
        level=level,
        source=str(body.get("source") or "external").strip(),
    )
    return ok({"success": True, "notice": content})
