"""Reflect the caller's Origin onto the response for allowlisted origins.

Why this exists: the login session now travels in cookies, and a *credentialed*
cross-origin fetch is refused by the browser when the response carries
``Access-Control-Allow-Origin: *`` — which is exactly what ``ALLOW_CORS``
installs for a multi-origin allowlist. Echoing the origin (never ``*``) plus the
``Access-Control-Allow-Credentials`` header satisfies the credentialed-CORS
rules without widening who may talk to the API: the gateway already refuses any
Origin outside the allowlist before a handler runs, so the only origins ever
echoed here are the configured ones.

Robyn's ``AFTER_REQUEST`` handlers receive ``(request, response)`` and must
return the response; the header write is a no-op for callers that sent no
Origin (the desktop webview's own fetch is same-origin to its bundle).
"""

from __future__ import annotations

from robyn import Request, Response

from server.trigger import auth

__all__ = ["cors_origin_echo"]


def cors_origin_echo(request: Request, response: Response) -> Response:
    """Set the exact-origin CORS header when the caller's Origin is allowlisted."""
    origin = (request.headers.get("Origin") or "").strip()
    if origin and origin in auth.allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response
