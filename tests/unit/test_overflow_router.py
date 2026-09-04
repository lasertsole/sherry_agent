"""Unit tests for pub_func/message/overflow_router.py (Task 3, TDD).

Covers the 4-route overflow decision table (compute_pressure,
find_truncatable_tool_results, decide_route) including exact-threshold
boundaries for both soft and hard overflow zones, and the truncatable
candidate rules (recent-skip window, minimum-token floor, descending
order, non-ToolMessage exclusion).

All payloads are ASCII: estimate_msg_tokens uses CHARS_PER_TOKEN=4 and
underestimates CJK, so tests build content of exact char lengths to hit
deterministic token counts.
"""

from config.num import (
    CHARS_PER_TOKEN,
    COMPRESSION_TRIGGER_RATIO,
    MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE,
    PREEMPTIVE_TRUNCATE_RATIO,
    TRUNCATABLE_RECENT_SKIP,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens
from pub_func.message.overflow_router import (
    compute_pressure,
    decide_route,
    find_truncatable_tool_results,
)

ROUTE_FITS = "fits"
ROUTE_TRUNCATE_ONLY = "truncate_tool_results_only"
ROUTE_COMPACT_THEN_TRUNC = "compact_then_truncate"
ROUTE_COMPACT_ONLY = "compact_only"

# Real-window shape used by the plan QA scenario (W=65536, B=65536-16000).
CONTEXT_WINDOW = 65536
USABLE_BUDGET = 65536 - 16000


def _chars_for(tokens: int, call_id: str) -> str:
    """ASCII content whose estimate_msg_tokens is exactly ``tokens``.

    estimate_msg_tokens counts len(content) + len(tool_call_id), floor-divided
    by CHARS_PER_TOKEN — so content length = tokens*4 - len(call_id) hits the
    token count exactly.
    """
    return "x" * (tokens * CHARS_PER_TOKEN - len(call_id))


def _tool(tokens: int, call_id: str) -> ToolMessage:
    return ToolMessage(content=_chars_for(tokens, call_id), tool_call_id=call_id)


# ---------------------------------------------------------------------------
# compute_pressure
# ---------------------------------------------------------------------------


class TestComputePressure:
    def test_reported_none_uses_estimated_plus_system(self):
        assert compute_pressure(100, None) == 100
        assert compute_pressure(100, None, 30) == 130

    def test_reported_wins_when_present(self):
        # Reported (T3 API usage) value takes precedence over the estimate.
        assert compute_pressure(100, 500, 30) == 500

    def test_estimated_plus_system_wins_over_small_reported(self):
        assert compute_pressure(500, 100, 50) == 550

    def test_default_system_prompt_tokens_is_zero(self):
        assert compute_pressure(42, None) == 42


# ---------------------------------------------------------------------------
# decide_route — 4 routes + exact-threshold boundaries
# ---------------------------------------------------------------------------


class TestDecideRoute:
    # --- "fits" ------------------------------------------------------------

    def test_fits_low_pressure(self):
        assert decide_route(1000, CONTEXT_WINDOW, USABLE_BUDGET, []) == ROUTE_FITS

    def test_fits_half_budget(self):
        assert (
            decide_route(int(USABLE_BUDGET * 0.5), CONTEXT_WINDOW, USABLE_BUDGET, [])
            == ROUTE_FITS
        )

    def test_fits_just_below_truncate_threshold(self):
        # B=1000 -> threshold_truncate=700.0; 699 is the last fits step.
        assert decide_route(699, 2000, 1000, []) == ROUTE_FITS

    # --- soft overflow zone [0.70*B, 0.80*B) -------------------------------

    def test_soft_zone_with_candidates_truncates(self):
        assert (
            decide_route(
                int(USABLE_BUDGET * 0.75),
                CONTEXT_WINDOW,
                USABLE_BUDGET,
                [(0, 5000)],
            )
            == ROUTE_TRUNCATE_ONLY
        )

    def test_soft_zone_empty_candidates_fits(self):
        assert (
            decide_route(int(USABLE_BUDGET * 0.75), CONTEXT_WINDOW, USABLE_BUDGET, [])
            == ROUTE_FITS
        )

    def test_soft_lower_boundary_exactly_at_truncate_threshold(self):
        # pressure == 0.70*B exactly enters the soft zone (not "fits").
        assert decide_route(700, 2000, 1000, [(0, 300)]) == ROUTE_TRUNCATE_ONLY

    def test_soft_upper_boundary_just_below_compact_threshold(self):
        # 799 is the last step below 0.80*B=800 -> still soft zone.
        assert decide_route(799, 2000, 1000, [(0, 300)]) == ROUTE_TRUNCATE_ONLY

    def test_soft_upper_boundary_empty_candidates_fits(self):
        assert decide_route(799, 2000, 1000, []) == ROUTE_FITS

    # --- hard overflow zone pressure >= 0.80*B -----------------------------

    def test_hard_lower_boundary_exactly_compact_threshold_under_budget(self):
        # pressure == 0.80*B=800 but still below B=1000: overflow is negative,
        # any truncatable candidate suffices -> truncate only.
        assert decide_route(800, 2000, 1000, [(0, 500)]) == ROUTE_TRUNCATE_ONLY

    def test_hard_overflow_zero_boundary_with_candidates(self):
        # pressure == B exactly: overflow == 0, truncatable sum >= 0.
        assert decide_route(1000, 2000, 1000, [(0, 500)]) == ROUTE_TRUNCATE_ONLY

    def test_hard_overflow_zero_boundary_no_candidates(self):
        assert decide_route(1000, 2000, 1000, []) == ROUTE_COMPACT_ONLY

    def test_hard_truncate_sufficient(self):
        assert (
            decide_route(
                USABLE_BUDGET + 5000,
                CONTEXT_WINDOW,
                USABLE_BUDGET,
                [(0, 8000)],
            )
            == ROUTE_TRUNCATE_ONLY
        )

    def test_hard_sum_exactly_equals_overflow_truncates(self):
        # overflow = 5000; candidate sum == 5000 hits the >= boundary.
        assert (
            decide_route(
                USABLE_BUDGET + 5000,
                CONTEXT_WINDOW,
                USABLE_BUDGET,
                [(0, 2000), (1, 3000)],
            )
            == ROUTE_TRUNCATE_ONLY
        )

    def test_hard_sum_just_below_overflow_compact_then_truncate(self):
        assert (
            decide_route(
                USABLE_BUDGET + 5000,
                CONTEXT_WINDOW,
                USABLE_BUDGET,
                [(0, 2000), (1, 2999)],
            )
            == ROUTE_COMPACT_THEN_TRUNC
        )

    def test_hard_compact_then_truncate(self):
        assert (
            decide_route(
                USABLE_BUDGET + 5000,
                CONTEXT_WINDOW,
                USABLE_BUDGET,
                [(0, 3000)],
            )
            == ROUTE_COMPACT_THEN_TRUNC
        )

    def test_hard_no_candidates_compact_only(self):
        assert (
            decide_route(USABLE_BUDGET + 5000, CONTEXT_WINDOW, USABLE_BUDGET, [])
            == ROUTE_COMPACT_ONLY
        )

    def test_thresholds_derive_from_config_ratios(self):
        # The soft/hard split must sit at usable_budget * config ratios, not
        # at hard-coded numbers: with the real ratios the soft zone starts at
        # 0.70*B and the hard zone at 0.80*B.
        assert int(USABLE_BUDGET * PREEMPTIVE_TRUNCATE_RATIO) < int(
            USABLE_BUDGET * COMPRESSION_TRIGGER_RATIO
        )
        # Just below the 0.80*B band (float threshold 39628.8) -> soft zone.
        assert decide_route(
            int(USABLE_BUDGET * COMPRESSION_TRIGGER_RATIO) - 1,
            CONTEXT_WINDOW,
            USABLE_BUDGET,
            [],
        ) == ROUTE_FITS
        # Clearly above the band -> hard overflow, no candidates.
        assert decide_route(
            int(USABLE_BUDGET * 0.90),
            CONTEXT_WINDOW,
            USABLE_BUDGET,
            [],
        ) == ROUTE_COMPACT_ONLY


# ---------------------------------------------------------------------------
# find_truncatable_tool_results — candidate rules
# ---------------------------------------------------------------------------


class TestFindTruncatableToolResults:
    def test_recent_six_skipped_and_mid_candidate_selected(self):
        """Plan QA scenario: 12 messages, the last 6 all carry huge tool
        results, index 5 (just outside the recent window) holds a 250-token
        tool result, index 0 holds a 100-token one (below the floor).

        Expected candidates: exactly [(5, 250)].
        """
        messages = [
            _tool(100, "t0"),  # too small -> filtered by floor
            HumanMessage(content="filler 1"),
            HumanMessage(content="filler 2"),
            AIMessage(content="filler 3"),
            HumanMessage(content="filler 4"),
            _tool(250, "t5"),  # the only eligible candidate
        ]
        for i in range(TRUNCATABLE_RECENT_SKIP):
            messages.append(_tool(2000, f"r{i}"))  # inside recent window

        assert len(messages) == 12
        assert find_truncatable_tool_results(messages) == [(5, 250)]

    def test_min_token_floor_excludes_just_below(self):
        below = MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE - 1  # 199 tokens
        messages = [_tool(below, "t0"), HumanMessage(content="hi")]
        assert find_truncatable_tool_results(messages) == []

    def test_min_token_floor_includes_exact_boundary(self):
        exact = MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE  # 200 tokens, >= boundary
        messages = [_tool(exact, "t0"), HumanMessage(content="hi")]
        # Pad so the candidate at index 0 sits outside the recent window.
        for i in range(TRUNCATABLE_RECENT_SKIP):
            messages.append(HumanMessage(content=f"tail {i}"))
        keep_until = len(messages) - TRUNCATABLE_RECENT_SKIP
        assert keep_until > 0
        assert find_truncatable_tool_results(messages) == [(0, exact)]

    def test_sorted_desc_by_estimated_tokens(self):
        messages = [
            _tool(400, "t0"),
            _tool(900, "t1"),
        ]
        for i in range(TRUNCATABLE_RECENT_SKIP):
            messages.append(_tool(2000, f"r{i}"))  # recent window, skipped

        result = find_truncatable_tool_results(messages)
        assert result == [(1, 900), (0, 400)]
        tokens = [est for _, est in result]
        assert tokens == sorted(tokens, reverse=True)

    def test_non_tool_messages_are_excluded(self):
        messages = [
            HumanMessage(content=_chars_for(5000, "")),
            AIMessage(content=_chars_for(5000, "")),
            HumanMessage(content=_chars_for(5000, "")),
            AIMessage(content=_chars_for(5000, "")),
            HumanMessage(content=_chars_for(5000, "")),
            AIMessage(content=_chars_for(5000, "")),
            HumanMessage(content=_chars_for(5000, "")),
        ]
        assert find_truncatable_tool_results(messages) == []

    def test_all_messages_inside_recent_window_returns_empty(self):
        messages = [_tool(2000, "t0"), _tool(2000, "t1"), _tool(2000, "t2")]
        assert len(messages) < TRUNCATABLE_RECENT_SKIP
        assert find_truncatable_tool_results(messages) == []

    def test_empty_messages_returns_empty(self):
        assert find_truncatable_tool_results([]) == []

    def test_tuples_match_estimate_msg_tokens(self):
        messages = [_tool(300, "t0"), HumanMessage(content="mid"), _tool(700, "t2")]
        # Pad to 9 messages so candidates at indices 0 and 2 are eligible.
        for i in range(TRUNCATABLE_RECENT_SKIP):
            messages.append(HumanMessage(content=f"tail {i}"))
        result = find_truncatable_tool_results(messages)
        assert result == [
            (2, estimate_msg_tokens(messages[2])),
            (0, estimate_msg_tokens(messages[0])),
        ]
