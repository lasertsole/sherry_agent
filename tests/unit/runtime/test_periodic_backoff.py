"""Unit tests for runtime.periodic_backoff.PeriodicBackoff.

TDD task 2 of .omo/plans/loop-detection-cron-breaker.md.
Covers: exponential doubling arithmetic, max_interval cap,
exhausted flag at max_consecutive_failures, reason recording,
full reset on record_success.
"""

import pytest

from runtime.periodic_backoff import PeriodicBackoff


class TestInitial_state:
    def test_fresh_instance_starts_at_base_interval(self):
        backoff = PeriodicBackoff(base_interval=60)

        assert backoff.current_interval == 60
        assert backoff.consecutive_failures == 0
        assert backoff.reason == ""
        assert backoff.exhausted is False
        assert backoff.is_exhausted() is False

    def test_default_field_values(self):
        backoff = PeriodicBackoff(base_interval=60)

        assert backoff.factor == 2.0
        assert backoff.max_interval == 7200.0
        assert backoff.max_consecutive_failures == 5


class TestDoublingArithmetic:
    def test_interval_doubles_per_failure_uncapped_region(self):
        backoff = PeriodicBackoff(base_interval=60)

        expected = [120, 240, 480, 960, 1920, 3840]
        observed = []
        for _ in range(len(expected)):
            backoff.record_failure("fail")
            observed.append(backoff.current_interval)

        assert observed == expected

    def test_interval_capped_at_max_interval(self):
        backoff = PeriodicBackoff(base_interval=60)

        observed = []
        for _ in range(12):
            backoff.record_failure("fail")
            observed.append(backoff.current_interval)

        # 60 * 2^7 == 7680 -> capped to 7200 at the 7th failure onwards.
        assert observed == [
            120, 240, 480, 960, 1920, 3840,
            7200, 7200, 7200, 7200, 7200, 7200,
        ]

    def test_custom_factor_and_max_interval(self):
        backoff = PeriodicBackoff(base_interval=10, factor=3.0, max_interval=100)

        backoff.record_failure("fail")
        assert backoff.current_interval == 30
        backoff.record_failure("fail")
        assert backoff.current_interval == 90
        backoff.record_failure("fail")
        # 10 * 3^3 == 270 -> capped at 100.
        assert backoff.current_interval == 100


class TestExhaustedFlag:
    def test_exhausted_at_max_consecutive_failures(self):
        backoff = PeriodicBackoff(base_interval=60)

        for i in range(4):
            backoff.record_failure(f"fail {i}")
            assert backoff.is_exhausted() is False

        backoff.record_failure("fail 4")
        assert backoff.is_exhausted() is True
        assert backoff.exhausted is True

    def test_exhausted_stays_true_on_further_failures(self):
        backoff = PeriodicBackoff(base_interval=60)

        for _ in range(6):
            backoff.record_failure("fail")

        assert backoff.is_exhausted() is True
        assert backoff.consecutive_failures == 6


class TestReasonRecording:
    def test_reason_recorded_on_failure(self):
        backoff = PeriodicBackoff(base_interval=60)

        backoff.record_failure("cron job exited with code 1")

        assert backoff.reason == "cron job exited with code 1"

    def test_reason_keeps_latest_failure(self):
        backoff = PeriodicBackoff(base_interval=60)

        backoff.record_failure("first failure")
        backoff.record_failure("second failure")

        assert backoff.reason == "second failure"


class TestSuccessReset:
    def test_record_success_resets_everything(self):
        backoff = PeriodicBackoff(base_interval=60)
        backoff.record_failure("fail 1")
        backoff.record_failure("fail 2")
        backoff.record_failure("fail 3")
        assert backoff.current_interval == 480

        backoff.record_success()

        assert backoff.current_interval == 60
        assert backoff.consecutive_failures == 0
        assert backoff.exhausted is False
        assert backoff.is_exhausted() is False
        assert backoff.reason == ""

    def test_record_success_after_exhausted_allows_new_cycle(self):
        backoff = PeriodicBackoff(base_interval=60)
        for _ in range(5):
            backoff.record_failure("fail")
        assert backoff.is_exhausted() is True

        backoff.record_success()

        assert backoff.is_exhausted() is False
        backoff.record_failure("new cycle failure")
        assert backoff.current_interval == 120
        assert backoff.is_exhausted() is False
