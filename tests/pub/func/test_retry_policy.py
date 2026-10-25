"""Retry delay policy: ZCode-parity math and Retry-After parsing."""

from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, UTC

import pytest

from pub.func.retry_policy import (
    compute_retry_delay,
    extract_retry_after_seconds,
    parse_retry_after_seconds,
)

pytestmark = pytest.mark.unit


def test_exponential_delay_is_capped() -> None:
    assert compute_retry_delay(1, base_delay=2.0, jitter_mode="none") == 2.0
    assert compute_retry_delay(2, base_delay=2.0, jitter_mode="none") == 4.0
    assert compute_retry_delay(4, base_delay=2.0, jitter_mode="none") == 16.0
    assert compute_retry_delay(10, base_delay=2.0, max_delay=60.0, jitter_mode="none") == 60.0


def test_full_jitter_stays_inside_half_to_full(monkeypatch) -> None:
    monkeypatch.setattr("pub.func.retry_policy.random.random", lambda: 0.0)
    assert compute_retry_delay(3, base_delay=2.0, jitter_mode="full") == 4.0
    monkeypatch.setattr("pub.func.retry_policy.random.random", lambda: 1.0)
    assert compute_retry_delay(3, base_delay=2.0, jitter_mode="full") == 8.0


def test_symmetric_jitter_can_vary_both_ways(monkeypatch) -> None:
    monkeypatch.setattr("pub.func.retry_policy.random.random", lambda: 0.0)
    low = compute_retry_delay(2, base_delay=2.0, jitter_mode="symmetric", jitter=0.3)
    monkeypatch.setattr("pub.func.retry_policy.random.random", lambda: 1.0)
    high = compute_retry_delay(2, base_delay=2.0, jitter_mode="symmetric", jitter=0.3)
    assert low == pytest.approx(2.8)
    assert high == pytest.approx(5.2)


def test_retry_after_wins_and_is_capped() -> None:
    assert (
        compute_retry_delay(
            1,
            base_delay=2.0,
            retry_after_s=17.0,
            retry_after_cap_s=300.0,
        )
        == 17.0
    )
    # Over the cap: fall back to the local curve (no endless sleep).
    assert (
        compute_retry_delay(
            1,
            base_delay=2.0,
            retry_after_s=10_000.0,
            retry_after_cap_s=300.0,
            jitter_mode="none",
        )
        == 2.0
    )


def test_parse_retry_after_formats() -> None:
    assert parse_retry_after_seconds({"retry-after": "12"}) == 12.0
    assert parse_retry_after_seconds({"Retry-After": "2.5"}) == 2.5
    assert parse_retry_after_seconds({"retry-after-ms": "1500"}) == 1.5
    assert parse_retry_after_seconds({"retry-after": "not-a-date"}) is None
    assert parse_retry_after_seconds({"retry-after": "0"}) is None
    future = datetime.now(UTC) + timedelta(seconds=90)
    parsed = parse_retry_after_seconds({"retry-after": format_datetime(future, usegmt=True)})
    assert parsed is not None and 80 <= parsed <= 90


def test_extract_retry_after_follows_response_and_cause() -> None:
    class Response:
        headers = {"Retry-After": "9"}

    class ProviderError(Exception):
        response = Response()

    assert extract_retry_after_seconds(ProviderError("limited")) == 9.0

    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = ProviderError("limited")
    assert extract_retry_after_seconds(wrapper) == 9.0
