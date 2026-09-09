"""Unit tests for pub_func.retry_utils (LLM error-handling plan, module F)."""

import random

import pytest

from pub_func.retry_utils import adaptive_rate_limit_backoff, jittered_backoff

pytestmark = [pytest.mark.unit]


class TestJitteredBackoff:
    def test_first_attempt_is_base_delay_with_no_jitter(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.5)
        assert jittered_backoff(1, base_delay=2.0, jitter=0.3) == pytest.approx(2.0)

    def test_exponential_growth_attempt_3(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.5)
        assert jittered_backoff(3, base_delay=2.0, jitter=0.3) == pytest.approx(8.0)

    def test_negative_full_jitter_floors_at_0_1(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.0)
        # 2.0 - 0.6 = 1.4 stays above the floor; force below it with a tiny base.
        assert jittered_backoff(1, base_delay=0.1, jitter=0.9) == pytest.approx(0.1)

    def test_result_never_exceeds_max_delay(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 1.0)
        # attempt=20 saturates the exponential growth; +30% jitter must still cap.
        assert jittered_backoff(20, base_delay=2.0, max_delay=60.0, jitter=0.3) == pytest.approx(
            60.0
        )

    def test_result_is_monotonic(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.5)
        assert jittered_backoff(1) <= jittered_backoff(2) <= jittered_backoff(3)


class TestAdaptiveRateLimitBackoff:
    def test_retry_after_is_honored(self):
        assert adaptive_rate_limit_backoff(1, retry_after=17.0) == pytest.approx(17.0)

    def test_retry_after_capped_at_max_delay(self):
        assert adaptive_rate_limit_backoff(1, retry_after=999.0) == pytest.approx(120.0)

    def test_non_positive_retry_after_falls_back_to_jitter(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.5)
        assert adaptive_rate_limit_backoff(2, retry_after=0.0, base_delay=5.0) == pytest.approx(
            10.0
        )

    def test_none_retry_after_falls_back_to_jitter(self, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.5)
        assert adaptive_rate_limit_backoff(1, retry_after=None, base_delay=5.0) == pytest.approx(
            5.0
        )
