"""The agent-controllable browser, HTTP face (工具箱 → 浏览器, CDP mode).

Endpoints:
    GET  /browser/status
        -> {"success": true, "enabled": bool, "running": bool, "pid": int|null,
            "pages": int, "sessions": int}

        Never the debug port: the CDP endpoint stays inside the backend (C3).

    POST /browser/page   {"session_id": "...", "url": "https://…"}
        -> {"success": true, page, url, title, session_id}

        Opens a page owned by that session (the panel's "new tab").

    POST /browser/navigate  {"session_id": "...", "url": "...", "page": "p1"?}
        -> {"success": true, page, url, title, session_id, loaded: bool}

    POST /browser/close  {"session_id": "...", "page": "p1"?}
        -> {"success": true, page, closed: true}

With the feature off every route answers 404 (C4), and no Chromium is ever
spawned.
"""

from __future__ import annotations

from config.features import BROWSER_AGENT
from server.service.browser_cdp import CdpError
from server.service.browser_manager import BrowserManager, get_browser_manager
from server.trigger.core import app
from server.trigger.http.helpers import (
    bad_request,
    failure_detail,
    not_found,
    ok,
    read_body,
    to_text_response,
)


def _manager_or_none() -> BrowserManager | None:
    """The manager when the feature is on, else ``None`` (routes answer 404)."""
    if not BROWSER_AGENT["enabled"]:
        return None
    return get_browser_manager()


def _session_of(body: dict) -> str:
    return str(body.get("session_id") or "").strip()


@app.get("/browser/status")
async def browser_status_handler(request):
    """Report the feature switch and the live page count."""
    manager = _manager_or_none()
    if manager is None:
        return not_found("the browser feature is disabled")
    return ok(manager.status())


@app.post("/browser/page")
async def browser_page_handler(request):
    """Open a page for a session (the panel's new tab)."""
    manager = _manager_or_none()
    if manager is None:
        return not_found("the browser feature is disabled")
    body = read_body(request) or {}
    session_id = _session_of(body)
    url = str(body.get("url") or "about:blank").strip() or "about:blank"
    if not session_id:
        return bad_request("session_id is required")
    try:
        page = await manager.open_page(session_id, url)
    except CdpError as error:
        return to_text_response(500, {"success": False, "message": failure_detail(error)})
    return ok(page.info())


@app.post("/browser/navigate")
async def browser_navigate_handler(request):
    """Navigate one page (the address bar / the agent's browser_navigate)."""
    manager = _manager_or_none()
    if manager is None:
        return not_found("the browser feature is disabled")
    body = read_body(request) or {}
    session_id = _session_of(body)
    if not session_id:
        return bad_request("session_id is required")
    page_id = str(body.get("page") or "").strip() or None
    try:
        outcome = await manager.navigate(session_id, str(body.get("url") or ""), page_id)
    except ValueError as error:
        return bad_request(str(error))
    except KeyError as error:
        return not_found(str(error))
    except CdpError as error:
        return to_text_response(500, {"success": False, "message": failure_detail(error)})
    return ok(outcome)


@app.post("/browser/close")
async def browser_close_handler(request):
    """Close one page (the panel's tab close)."""
    manager = _manager_or_none()
    if manager is None:
        return not_found("the browser feature is disabled")
    body = read_body(request) or {}
    session_id = _session_of(body)
    if not session_id:
        return bad_request("session_id is required")
    page_id = str(body.get("page") or "").strip() or None
    try:
        outcome = await manager.close_page(session_id, page_id)
    except KeyError as error:
        return not_found(str(error))
    except CdpError as error:
        return to_text_response(500, {"success": False, "message": failure_detail(error)})
    return ok(outcome)
