"""Token-budgeted truncation: the same budget in every language, capped items."""

from __future__ import annotations

import pytest

from agent.middlewares.summarization.summary_doc import (
    SummaryDoc,
    cap_item_lengths,
    cap_summary_doc,
)
from pub.func.message.token_truncation import truncate_by_tokens

pytestmark = [pytest.mark.unit]


class TestTruncateByTokens:
    def test_english_budget_matches_the_old_character_cap(self):
        """500 tokens ≈ 2 000 ASCII characters (4 chars/token) — the old cap."""
        text = "a" * 10_000

        clipped = truncate_by_tokens(text, 500, head_ratio=0.6, tail_ratio=0.4)

        body = clipped.split("...")[0]
        assert 1_100 <= len(body) <= 1_300  # 60 % of ~2 000

    def test_chinese_budget_is_tighter_in_characters(self):
        """The same 500 tokens is ~1 000 Chinese characters (2 chars/token)."""
        english = truncate_by_tokens("a" * 10_000, 500)
        chinese = truncate_by_tokens("中" * 10_000, 500)

        assert len(chinese) < len(english)
        # The inner text (marker stripped) is roughly half the English span.
        assert len(chinese.split("...")[0]) <= len(english.split("...")[0]) * 0.7

    def test_short_text_is_returned_unchanged(self):
        text = "short enough"
        assert truncate_by_tokens(text, 500) == text

    def test_omission_marker_reports_the_dropped_characters(self):
        clipped = truncate_by_tokens("x" * 4_000, 100)

        assert "[truncated" in clipped
        assert "chars]..." in clipped

    def test_head_and_tail_never_overlap(self):
        clipped = truncate_by_tokens("abcde", 1, head_ratio=1.0, tail_ratio=1.0)

        assert "abcde" in clipped  # nothing to drop at this size
        assert clipped.count("abcde") == 1

    def test_zero_budget_is_a_noop(self):
        assert truncate_by_tokens("text", 0) == "text"


class TestSummaryDocItemCaps:
    def test_a_runaway_item_is_clipped(self):
        doc = SummaryDoc(completed=["x" * 5_000])

        capped = cap_summary_doc(doc)

        assert len(capped.completed[0]) < 5_000
        assert "truncated" in capped.completed[0]

    def test_item_caps_are_idempotent(self):
        once = cap_summary_doc(SummaryDoc(completed=["y" * 3_000]))

        twice = cap_summary_doc(once)

        assert twice.completed == once.completed

    def test_evicted_refs_are_never_clipped(self):
        """A clipped path cannot be used to read the payload back."""
        long_path = "workspace/sessions/sess-1/evicted/" + "x" * 900 + ".txt"
        doc = SummaryDoc(evicted_refs=[long_path])

        capped = cap_summary_doc(doc)

        assert capped.evicted_refs == [long_path]

    def test_short_items_keep_shorter_budgets(self):
        doc = SummaryDoc(next_steps=["z" * 3_000], completed=["w" * 3_000])

        capped = cap_summary_doc(doc)

        # next_steps (50 tokens) clips harder than completed (100 tokens).
        assert len(capped.next_steps[0]) < len(capped.completed[0])

    def test_the_item_count_cap_still_applies(self):
        doc = SummaryDoc(completed=[f"item {i}" for i in range(20)])

        capped = cap_summary_doc(doc)

        assert len(capped.completed) < 20

    def test_cap_item_lengths_is_a_pure_helper(self):
        assert cap_item_lengths(["short", "x" * 5_000], 50)[0] == "short"
