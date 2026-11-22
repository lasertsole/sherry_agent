"""Transport gate for the high-risk guard's sensitive ledger.

Why
---
``high_risk_ops.args_preview`` can contain values the caller passed to a high-
risk tool (a charge amount, a deploy target, a token in an argument). The panel
API must not hand that preview over a plaintext cross-host link: a network
sniffer on the path would read it. Two transports are acceptable:

* **same host** — the client reached this backend over loopback (the desktop
  webview dialing 127.0.0.1, or a local script). Nothing crosses the network.
* **encrypted** — the request arrived over HTTPS/WSS. When a reverse proxy
  (Fleet Ingress) terminates TLS, it sets ``X-Forwarded-Proto: https`` on the
  forwarded request; that header is the trust anchor for the encrypted case.

Anything else (a remote client on plain HTTP/WS, or a proxied request with no
encrypted outer hop) is **degraded**: the handler still answers 200 with an EMPTY
ledger and a human-readable notice, so the panel shows a clear message instead
of a transport error while leaking no operation data.

Proxy awareness
---------------
A proxied request's ``ip_addr`` is the proxy itself (often loopback, because
the tenant cell runs on the same host), so a bare loopback check would wrongly
allow a plaintext remote client. Once ``X-Forwarded-Proto`` or
``X-Forwarded-For`` is present we treat the call as proxied and require an
explicit ``https``/``wss`` forwarded scheme; the peer address no longer counts.

This is a transport-layer guard, complementary to (not a replacement for) the
gateway Origin/token, CSRF, and login gates in ``server/trigger/core.py``.
"""

from __future__ import annotations

from server.trigger.auth_user import is_loopback

__all__ = [
    "DEGRADED_MESSAGE",
    "degraded_response",
    "ledger_transport_allowed",
]

#: The notice surfaced when the transport is not safe for the ledger.
DEGRADED_MESSAGE = (
    "高危账本包含敏感信息，仅在同一主机或 HTTPS/WSS 加密连接下可用。"
    "当前连接不满足条件，已隐藏账本数据。"
    "The high-risk ledger is available only on the same host or over an "
    "encrypted HTTPS/WSS connection; data was withheld on this transport."
)

_ENCRYPTED = frozenset({"https", "wss"})


def _headers(request) -> dict[str, str]:
    """Case-insensitive header view; empty for requests without a header map."""
    raw = getattr(request, "headers", None)
    if raw is None:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in raw.items()}
    except (AttributeError, TypeError):
        return {}


def _host_header_is_loopback(headers: dict[str, str]) -> bool:
    from urllib.parse import urlsplit

    host = headers.get("host", "").strip().lower()
    if not host:
        return False
    # urlsplit's hostname strips any port and IPv6 brackets for us.
    hostname = urlsplit(f"//{host}").hostname or ""
    return hostname in {"localhost", "127.0.0.1", "::1"} or hostname.startswith("127.")


def ledger_transport_allowed(request) -> bool:
    """Whether *request* may read/write the sensitive ledger.

    Direct calls in unit tests pass a minimal fake request with no headers or
    peer address; those are treated as same-host (an in-process call never
    crosses a network). Real Robyn requests always carry headers.
    """
    headers = _headers(request)
    forwarded_proto = headers.get("x-forwarded-proto", "").strip().lower()
    # A comma-separated chain (proxy -> proxy) is valid if any hop was TLS.
    protos = {item.strip().lower() for item in forwarded_proto.split(",") if item.strip()}
    if protos & _ENCRYPTED:
        return True

    proxied = bool(headers.get("x-forwarded-for")) or bool(forwarded_proto)
    if proxied:
        # A proxy is in front: the peer address is the proxy, not the client.
        # Without an encrypted forwarded hop the outer link is untrusted.
        return False

    # No proxy headers: trust a loopback peer, or a loopback Host header (the
    # latter also covers requests whose peer address is not exposed).
    if is_loopback(getattr(request, "ip_addr", None)):
        return True
    if _host_header_is_loopback(headers):
        return True
    # A header-less in-process/test request is a local call.
    return not headers and not getattr(request, "ip_addr", None)


def degraded_response() -> dict:
    """The safe payload returned instead of ledger data on a bad transport.

    ``status``/``success`` mirror the normal API envelope so the panel can
    render it without a transport error; every data field is empty.
    """
    return {
        "success": True,
        "degraded": True,
        "message": DEGRADED_MESSAGE,
        "keywords": [],
        "ops": [],
    }
