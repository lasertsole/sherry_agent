"""Overflow routing pure functions (Task 3 — dual-track router).

Pure decision layer for the context-compression dual-track router: given a
token pressure and a list of truncatable tool-result candidates, decide one
of four routes. Execution (actual truncation / compression) lives in the
middleware (Task 5 consumes these strings as its dispatch contract) — this
module performs NO truncation, NO compression, NO I/O and keeps NO global
state.

Routes (dispatch contract, stable strings):
    "fits"                       — no action needed
    "truncate_tool_results_only" — lightweight: truncate big tool results
    "compact_then_truncate"      — compress history first, truncate as backstop
    "compact_only"               — heavy: AI compression of the whole history

Thresholds are imported from ``config.num`` — never redefined here:
    PREEMPTIVE_TRUNCATE_RATIO (0.70) → soft-overflow lower bound
    COMPRESSION_TRIGGER_RATIO (0.80) → hard-overflow lower bound
    MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (200) → candidate floor
    TRUNCATABLE_RECENT_SKIP (6) → most recent messages never truncated

Soft/hard overflow semantics (软/硬溢出):
    软溢出 (threshold_truncate <= pressure < threshold_compact): pressure is
    above the preemptive-truncate line but still inside usable_budget.
    够截断就截、不够不动 — if any truncatable candidate exists, route to
    truncation only; otherwise do nothing ("fits"). Compression is never
    triggered by soft overflow alone.
    硬溢出 (pressure >= threshold_compact): the context must be pressed back
    below usable_budget (必须压回 usable_budget 以下). If the truncatable
    token sum covers the overflow, truncation alone suffices; if there are
    candidates but not enough tokens, compression runs first with truncation
    as backstop; with no candidates at all, only compression can help.
"""

from langchain_core.messages import BaseMessage, ToolMessage
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens
from config.num import (
    COMPRESSION_TRIGGER_RATIO,
    MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE,
    PREEMPTIVE_TRUNCATE_RATIO,
    TRUNCATABLE_RECENT_SKIP,
)

ROUTE_FITS = "fits"
ROUTE_TRUNCATE_TOOL_RESULTS_ONLY = "truncate_tool_results_only"
ROUTE_COMPACT_THEN_TRUNCATE = "compact_then_truncate"
ROUTE_COMPACT_ONLY = "compact_only"


def compute_pressure(
    estimated_tokens: int,
    reported_tokens: int | None,
    system_prompt_tokens: int = 0,
) -> int:
    """Token pressure = max(estimated + system_prompt, reported).

    The character-based estimate (CHARS_PER_TOKEN=4) can underestimate real
    usage; when the API reported actual token usage (T3 ``reported_tokens``),
    the reported value wins. With no report, fall back to the local estimate
    plus the system-prompt overhead.
    """
    pressure = estimated_tokens + system_prompt_tokens
    if reported_tokens is not None:
        pressure = max(pressure, reported_tokens)
    return pressure


def find_truncatable_tool_results(
    messages: list[BaseMessage],
) -> list[tuple[int, int]]:
    """Collect truncatable ToolMessage candidates.

    Rules:
      - ONLY ``ToolMessage`` instances are eligible (tool results are
        regenerable; the rest of the transcript is not).
      - The most recent ``TRUNCATABLE_RECENT_SKIP`` messages are skipped
        (index >= len(messages) - TRUNCATABLE_RECENT_SKIP is excluded) so
        the newest tool/ai pairing always stays intact — this preserves the
        pairing safety margin of the last ~2 turns.
      - A candidate must be worth it: est_tokens >=
        MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (via ``estimate_msg_tokens``).
      - Result is sorted DESC by estimated tokens so executors cut the
        biggest wins first.

    Returns ``[(message_index, est_tokens), ...]`` — original indices into
    ``messages``, so the middleware can replace them in place.
    """
    keep_until = len(messages) - TRUNCATABLE_RECENT_SKIP
    candidates: list[tuple[int, int]] = []
    for index, message in enumerate(messages):
        if index >= keep_until:
            continue
        if not isinstance(message, ToolMessage):
            continue
        est_tokens = estimate_msg_tokens(message)
        if est_tokens < MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE:
            continue
        candidates.append((index, est_tokens))
    candidates.sort(key=lambda candidate: candidate[1], reverse=True)
    return candidates


def decide_route(
    pressure_tokens: int,
    context_window: int,
    usable_budget: int,
    truncatable: list[tuple[int, int]],
) -> str:
    """4-route dual-track decision (pure — execution happens in middleware).

    thresholds derive from ``usable_budget`` (context_window minus reserve):
      threshold_truncate = usable_budget * PREEMPTIVE_TRUNCATE_RATIO (0.70)
      threshold_compact  = usable_budget * COMPRESSION_TRIGGER_RATIO (0.80)

    decision table:
      pressure < threshold_truncate
          → "fits"
      soft overflow (threshold_truncate <= pressure < threshold_compact):
          够截断就截、不够不动 — candidates exist →
          "truncate_tool_results_only"; none → "fits" (compression is never
          triggered by soft overflow alone)
      hard overflow (pressure >= threshold_compact):
          必须压回 usable_budget 以下 — overflow = pressure − usable_budget;
          candidate token sum >= overflow → "truncate_tool_results_only";
          0 < sum < overflow → "compact_then_truncate"; no candidates →
          "compact_only"

    ``context_window`` is part of the dispatch signature (Task 5 passes it
    through) but the band math is intentionally budget-relative: the reserve
    between usable_budget and context_window is already accounted for by the
    caller.
    """
    threshold_truncate = usable_budget * PREEMPTIVE_TRUNCATE_RATIO
    threshold_compact = usable_budget * COMPRESSION_TRIGGER_RATIO

    if pressure_tokens < threshold_truncate:
        return ROUTE_FITS

    if pressure_tokens < threshold_compact:
        # Soft overflow: truncation is opportunistic, never compels compression.
        if truncatable:
            return ROUTE_TRUNCATE_TOOL_RESULTS_ONLY
        return ROUTE_FITS

    # Hard overflow: pressure must be pressed back below usable_budget.
    if not truncatable:
        return ROUTE_COMPACT_ONLY
    overflow = pressure_tokens - usable_budget
    truncatable_tokens = sum(est_tokens for _, est_tokens in truncatable)
    if truncatable_tokens >= overflow:
        return ROUTE_TRUNCATE_TOOL_RESULTS_ONLY
    return ROUTE_COMPACT_THEN_TRUNCATE
