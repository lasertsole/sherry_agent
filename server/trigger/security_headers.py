"""Static security response headers (CSP, nosniff, framing, referrer).

Why a server-side layer when the client already sanitizes: DOMPurify runs in the
same context as the payload it sanitizes, so a bypass (0day, a config regression)
executes. ``script-src 'self'`` is enforced by the browser instead, and it costs
nothing on responses that carry no markup.

The policy is deliberately permissive where this app legitimately needs it:

* ``style-src 'unsafe-inline'`` — Vue and the GFM table renderer set inline
  styles (the client already gates style *values* through its own regex);
* ``img-src data: blob:`` / ``media-src blob:`` — uploads and generated media
  are handed to the page as data URLs and object URLs;
* ``connect-src`` lists the loopback forms the client actually dials (the WS
  gateway on 127.0.0.1 and [::1], any port).

``X-Content-Type-Options: nosniff`` matters most for the static media routes:
they serve user-uploaded bytes with a guessed content type, and a browser that
sniffs an uploaded file as HTML would run it in this origin.

``GATEWAY["csp"]`` selects the policy: empty (the default) uses the policy
below, ``"disabled"`` omits the header entirely, anything else is used verbatim
— so a deployment whose frontend needs a different shape is one env var away.
The other headers are fixed. The values are read per call, never snapshotted at
import time (an import-time snapshot of a config value is exactly the failure
mode the gateway config documents).
"""

from __future__ import annotations

from config.features import GATEWAY

__all__ = ["DEFAULT_CSP", "security_headers"]

#: The policy for the shipped client (see the module docstring for each grant).
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:* ws://[::1]:* "
    "wss://127.0.0.1:* wss://localhost:* wss://[::1]:*; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_FIXED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def security_headers() -> dict[str, str]:
    """The headers to set on every response, with the CSP from config."""
    configured = str(GATEWAY.get("csp", "") or "").strip()
    headers = dict(_FIXED_HEADERS)
    if configured.lower() != "disabled":
        headers["Content-Security-Policy"] = configured or DEFAULT_CSP
    return headers
