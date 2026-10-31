"""Inbound WS rate limits (design concept #28): two fail-open token buckets.

- ``consume_frame`` gates one inbound ``/sessions/ws`` frame per connection:
  burst 50, sustained 10 frames/s, and the ``rate_limited`` notice rides the
  TRANSITION into starvation (one reply per burst, never per dropped frame).
- ``consume_ws_ticket`` gates ``/auth/ws-ticket`` mints per client address
  (30/minute) — the login throttle's sibling for the ticket flow.
Both restore unlimited behavior when ``WS_RATE_LIMIT["enabled"]`` is False and
allow everything when they themselves raise (a limiter must not take the
socket or the login flow down).
"""

from __future__ import annotations

import pytest

from config.features import WS_RATE_LIMIT
from server.trigger import rate_limit

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


@pytest.fixture(autouse=True)
def _enabled_and_isolated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(WS_RATE_LIMIT, "enabled", True)
    rate_limit._frames.clear()
    rate_limit._tickets.clear()
    yield
    rate_limit._frames.clear()
    rate_limit._tickets.clear()


def _spend(socket_id: str, n: int) -> list[tuple[bool, bool]]:
    return [rate_limit.consume_frame(socket_id) for _ in range(n)]


def test_the_burst_is_allowed_then_dropped_with_one_notice() -> None:
    results = _spend("sock-1", int(WS_RATE_LIMIT["burst"]) + 5)

    assert all(allowed for allowed, _ in results[: WS_RATE_LIMIT["burst"]])
    dropped = results[WS_RATE_LIMIT["burst"] :]
    assert all(not allowed for allowed, _ in dropped)
    assert [notify for _, notify in dropped] == [True] + [False] * (len(dropped) - 1), (
        "exactly ONE notice: the transition into starvation"
    )


def test_buckets_are_per_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(WS_RATE_LIMIT, "burst", 1)
    monkeypatch.setitem(WS_RATE_LIMIT, "frames_per_second", 0.0)

    assert rate_limit.consume_frame("sock-a") == (True, False)
    assert rate_limit.consume_frame("sock-a") == (False, True)
    assert rate_limit.consume_frame("sock-b") == (True, False), "another socket, own bucket"


def test_time_refills_the_frame_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.trigger.rate_limit as rl

    monkeypatch.setitem(WS_RATE_LIMIT, "burst", 1)
    monkeypatch.setitem(WS_RATE_LIMIT, "frames_per_second", 10.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["now"])

    assert rl.consume_frame("sock-1")[0] is True
    assert rl.consume_frame("sock-1")[0] is False
    clock["now"] += 0.2  # 10 frames/s → 2 tokens accrued, capped at burst=1

    assert rl.consume_frame("sock-1")[0] is True


def test_the_ticket_bucket_answers_429_only_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(WS_RATE_LIMIT, "ws_tickets_per_minute", 2)

    assert rate_limit.consume_ws_ticket("10.0.0.1") is True
    assert rate_limit.consume_ws_ticket("10.0.0.1") is True
    assert rate_limit.consume_ws_ticket("10.0.0.1") is False
    assert rate_limit.consume_ws_ticket("10.0.0.2") is True, "per ADDRESS, not global"


def test_a_disabled_limiter_restores_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(WS_RATE_LIMIT, "enabled", False)

    for _ in range(200):
        assert rate_limit.consume_frame("sock-1")[0] is True
        assert rate_limit.consume_ws_ticket("10.0.0.1") is True


def test_a_limiter_crash_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.trigger.rate_limit as rl

    def _boom(*_args, **_kwargs):
        raise RuntimeError("clock exploded")

    monkeypatch.setattr(rl.time, "monotonic", _boom)

    assert rl.consume_frame("sock-1") == (True, False)
    assert rl.consume_ws_ticket("10.0.0.1") is True
