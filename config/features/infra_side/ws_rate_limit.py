"""Inbound WS rate limits: frame bucket and ticket-mint bucket (see server/trigger/rate_limit.py)."""

from typing import TypedDict


class WsRateLimitConfig(TypedDict):
    """Inbound WS rate limits (design concept #28; fail-open, process-local)."""

    #: Master switch. False restores the unlimited behavior entirely.
    enabled: bool
    #: Sustained inbound frames per second, per /sessions/ws connection.
    frames_per_second: float
    #: Bucket capacity — the burst a client may spend at once.
    burst: int
    #: /auth/ws-ticket issuances per minute, per client address.
    ws_tickets_per_minute: int


WS_RATE_LIMIT: WsRateLimitConfig = {
    "enabled": True,
    "frames_per_second": 10.0,
    "burst": 50,
    "ws_tickets_per_minute": 30,
}
