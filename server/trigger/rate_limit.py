"""WS inbound rate limits: per-connection frames and per-address ticket minting.

Design concept #28 (rate limiting) + #25 (WebSockets): the ONLY limiter that
existed was the login throttle — an authenticated socket could flood inbound
frames (worst case a self-DoS: each frame costs a full tool turn), and
``/auth/ws-ticket`` had no bound at all. Two token buckets close that:

- ``consume_frame`` — one inbound ``/sessions/ws`` frame per connection
  (sustained 10 frames/s, burst 50);
- ``consume_ws_ticket`` — one ticket mint per client address
  (30 per minute).

Threat model honesty: everything past the gateway is "one of us" (loopback +
per-boot token), so this is defense in depth, not a wall. Both buckets are
FAIL-OPEN — a limiter bug must never take the socket or the ticket flow down —
and process-local (like the LLM sticky fallback), never a session register.
"""

from __future__ import annotations

import time

from loguru import logger

from config.features import WS_RATE_LIMIT

__all__ = ["consume_frame", "consume_ws_ticket"]

_FRAME_PRUNE_SIZE = 4096
_FRAME_STALE_S = 300.0


class _Bucket:
    """A refillable token bucket (monotonic clock) plus its starvation edge."""

    __slots__ = ("tokens", "last", "starved")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens = capacity
        self.last = now
        self.starved = False

    def refill(self, capacity: float, rate_per_s: float, now: float) -> None:
        self.tokens = min(capacity, self.tokens + (now - self.last) * rate_per_s)
        self.last = now


_frames: dict[str, _Bucket] = {}
_tickets: dict[str, _Bucket] = {}


def _prune(buckets: dict[str, _Bucket], now: float) -> None:
    """Drop buckets idle long enough that they are certainly dead sockets."""
    if len(buckets) < _FRAME_PRUNE_SIZE:
        return
    stale = [key for key, bucket in buckets.items() if now - bucket.last > _FRAME_STALE_S]
    for key in stale:
        buckets.pop(key, None)


def consume_frame(socket_id: str) -> tuple[bool, bool]:
    """One inbound ``/sessions/ws`` frame.

    Returns ``(allowed, notify)``: a dropped frame sets ``notify`` exactly when
    the socket just transitioned into starvation (its previous frame was
    allowed), so the reply is ONE ``rate_limited`` frame per burst — replying
    per dropped frame would amplify the flood onto the socket doing it.
    """
    if not WS_RATE_LIMIT["enabled"]:
        return True, False
    try:
        now = time.monotonic()
        capacity = float(WS_RATE_LIMIT["burst"])
        rate = float(WS_RATE_LIMIT["frames_per_second"])
        bucket = _frames.get(str(socket_id))
        if bucket is None:
            bucket = _Bucket(capacity, now)
            _frames[str(socket_id)] = bucket
        bucket.refill(capacity, rate, now)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            bucket.starved = False
            return True, False
        notify = not bucket.starved
        bucket.starved = True
        if notify:
            logger.warning(
                "WS frame rate limited: socket={} rate={}/s burst={}", socket_id, rate, capacity
            )
        return False, notify  # the transition edge is the ONE notice per burst
    except Exception:  # noqa: BLE001 — the limiter must never break the socket
        return True, False


def consume_ws_ticket(ip_addr: str) -> bool:
    """One ``/auth/ws-ticket`` mint per client address. False = over the cap."""
    if not WS_RATE_LIMIT["enabled"]:
        return True
    try:
        now = time.monotonic()
        cap = float(WS_RATE_LIMIT["ws_tickets_per_minute"])
        key = str(ip_addr or "unknown")
        bucket = _tickets.get(key)
        if bucket is None:
            bucket = _Bucket(cap, now)
            _tickets[key] = bucket
        bucket.refill(cap, cap / 60.0, now)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        logger.warning("WS ticket rate limited: addr={} per_minute={}", key, cap)
        return False
    except Exception:  # noqa: BLE001 — the limiter must never break the ticket flow
        return True
    finally:
        try:
            _prune(_tickets, time.monotonic())
        except Exception:  # noqa: BLE001, S110 — pruning must not mask the fail-open answer
            pass
