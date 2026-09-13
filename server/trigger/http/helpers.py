"""Shared Robyn HTTP response helpers (audit 2.1.6).

``server/trigger/http/subagent.py`` and ``server/trigger/http/cron.py``
previously carried identical private ``_to_text_response`` / ``_ok`` /
``_bad_request`` / ``_not_found`` / ``_read_body`` sets, and
``server/trigger/http/skills.py`` a matching ``_json_response``. They are
extracted here verbatim; each endpoint module re-exports them under its
original private names so call sites (and tests that patch those names) are
unaffected.
"""

import json

from robyn import Response


def to_text_response(status_code: int, payload: dict) -> Response:
    """Build a JSON Robyn Response."""
    return Response(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        description=json.dumps(payload, ensure_ascii=False),
    )


def ok(payload: dict) -> Response:
    return to_text_response(200, payload)


def bad_request(message: str) -> Response:
    return to_text_response(400, {"success": False, "message": message})


def not_found(message: str) -> Response:
    return to_text_response(404, {"success": False, "message": message})


def read_body(request) -> dict | None:
    """Parse a JSON request body defensively."""
    try:
        body = request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None
