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


def query_int(
    query,
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """
    Read an integer query parameter, clamped to the given bounds.

    Robyn's ``QueryParams.get`` casts its default argument to ``str`` and raises
    ``TypeError`` for anything else, so the obvious ``int(q.get(key, 500))`` form
    always raised and the surrounding ``except`` quietly produced the fallback —
    the parameter was accepted but never honored (`/knowledge-graph` max_depth /
    max_nodes, `/logs` lines). Pass the fallback as the string it really is and
    convert here.

    @param query Robyn ``request.query_params`` (or an equivalent mapping).
    @param key Parameter name.
    @param default Value used when the parameter is missing or unparsable.
    @param minimum Optional inclusive lower bound.
    @param maximum Optional inclusive upper bound.
    @returns The parsed (and clamped) integer.
    """
    try:
        value = int(query.get(key, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
