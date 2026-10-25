"""The absolute compression line: window - max_output, else 80% of window."""

from __future__ import annotations

import pytest

from agent.middlewares.summarization.thresholds import (
    resolve_compression_threshold,
    resolve_truncate_threshold,
)
from pub.func.message.overflow_router import (
    ROUTE_COMPACT_ONLY,
    ROUTE_FITS,
    ROUTE_TRUNCATE_TOOL_RESULTS_ONLY,
    decide_route,
)

pytestmark = [pytest.mark.unit]


class TestResolveCompressionThreshold:
    def test_unset_output_cap_keeps_the_legacy_eighty_percent(self):
        assert resolve_compression_threshold(100_000, None) == (80_000, 0.8)
        assert resolve_compression_threshold(100_000, 0) == (80_000, 0.8)

    def test_set_output_cap_uses_the_remaining_input_budget(self):
        assert resolve_compression_threshold(100_000, 10_000) == (90_000, 0.9)
        threshold, ratio = resolve_compression_threshold(200_000, 8_192)
        assert threshold == 191_808
        assert ratio == pytest.approx(191_808 / 200_000)

    def test_output_cap_at_or_above_the_window_clamps_to_zero(self):
        assert resolve_compression_threshold(100_000, 100_000) == (0, 0.0)
        assert resolve_compression_threshold(100_000, 120_000) == (0, 0.0)

    def test_invalid_window_is_zero(self):
        assert resolve_compression_threshold(0, 10_000) == (0, 0.0)
        assert resolve_compression_threshold(-1, None) == (0, 0.0)


class TestTruncateThreshold:
    def test_no_cap_keeps_seventy_percent_of_the_window(self):
        assert resolve_truncate_threshold(80_000) == 70_000

    def test_cap_scales_the_soft_line_proportionally(self):
        # 0.70 / 0.80 = 0.875 of the 90k compression line.
        assert resolve_truncate_threshold(90_000) == 78_750

    def test_zero_compression_line_has_no_soft_line(self):
        assert resolve_truncate_threshold(0) == 0


class TestRouterAbsoluteThresholds:
    def test_the_absolute_compression_line_drives_compact(self):
        # 850 is above the soft 700 line but below compact 900: no candidates
        # means stay put.
        assert (
            decide_route(850, 1_000, 1_000, [], compression_threshold=900, truncate_threshold=700)
            == ROUTE_FITS
        )
        assert (
            decide_route(
                850,
                1_000,
                1_000,
                [(0, 100)],
                compression_threshold=900,
                truncate_threshold=700,
            )
            == ROUTE_TRUNCATE_TOOL_RESULTS_ONLY
        )
        assert (
            decide_route(900, 1_000, 1_000, [], compression_threshold=900, truncate_threshold=700)
            == ROUTE_COMPACT_ONLY
        )

    def test_legacy_callers_keep_the_budget_relative_math(self):
        assert decide_route(799, 2_000, 1_000, []) == ROUTE_FITS
        assert decide_route(800, 2_000, 1_000, []) == ROUTE_COMPACT_ONLY
