"""Unit tests for the four pub_func/message tool utilities.

Covers the tool-level test classes documented in PART2 section 13 of the
summarization redesign:

- TestTurnUtils         (4 cases): split_into_turns correct splitting,
                                   splitting inside a turn
- TestToolOutputDedup   (6 cases): keep latest duplicate, protected_tools
                                   skipped, signature grouping
- TestToolOutputPrune   (5 cases): protect-window logic, summary stop,
                                   minimum-reduction gate
- TestTargetTruncation  (5 cases): largest-first ordering, stop at target,
                                   small outputs skipped

All tests use real langchain_core messages (the tools dispatch on
isinstance checks) and plain asserts, following the style of
tests/unit/test_message_utils.py. Windows-safe: ASCII only, no network.
"""

# pyright: reportUnknownParameterType=false
# pyright: reportArgumentType=false
# pyright: reportAny=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportCallIssue=false

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from pub_func.message.target_truncation import target_truncate_tool_outputs
from pub_func.message.tool_output_dedup import dedup_tool_outputs
from pub_func.message.tool_output_prune import prune_tool_outputs
from pub_func.message.turn_utils import split_into_turns, split_turn

DEDUP_PLACEHOLDER = "[Duplicated call to search - output cleared, see latest result]"
PRUNE_MARKER = "[Old tool result content cleared]"


def _ai_with_call(tc_id: str, name: str, args: dict, content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=[{"name": name, "args": args, "id": tc_id}])


# --- TestTurnUtils ---


class TestTurnUtils:
    def test_split_into_turns_empty_list(self):
        assert split_into_turns([]) == []

    def test_split_into_turns_none_input(self):
        # Guard clause `if not messages` also absorbs None input.
        assert split_into_turns(None) == []

    def test_split_into_turns_groups_by_human_boundary(self):
        sys = SystemMessage(content="system")
        h1, a1 = HumanMessage(content="q1"), AIMessage(content="r1")
        h2, a2 = HumanMessage(content="q2"), AIMessage(content="r2")
        turns = split_into_turns([sys, h1, a1, h2, a2])
        assert len(turns) == 3
        assert (turns[0].start_idx, turns[0].end_idx) == (0, 1)
        assert turns[0].messages == [sys]
        assert (turns[1].start_idx, turns[1].end_idx) == (1, 3)
        assert turns[1].messages == [h1, a1]
        assert (turns[2].start_idx, turns[2].end_idx) == (3, 5)
        assert turns[2].messages == [h2, a2]

    def test_split_turn_within_turn_finds_split_point(self):
        a1 = AIMessage(content="a" * 100)
        a2 = AIMessage(content="b" * 100)
        turn = split_into_turns([HumanMessage(content="q"), a1, a2])[0]
        estimator = lambda msgs: sum(len(str(m.content)) for m in msgs) // 4
        # Only the last message fits a 25-token budget -> split before a2.
        assert split_turn(turn, 25, estimator) == 2
        # Whole tail fits a 50-token budget -> split right after the Human turn head.
        assert split_turn(turn, 50, estimator) == 1
        # Non-positive budget and single-message turns cannot be split.
        assert split_turn(turn, 0, estimator) is None
        single = split_into_turns([HumanMessage(content="q")])[0]
        assert split_turn(single, 100, estimator) is None


# --- TestToolOutputDedup ---


class TestToolOutputDedup:
    def test_duplicate_calls_keep_latest_output(self):
        messages = [
            HumanMessage(content="q"),
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="x" * 1000, tool_call_id="c1"),
            _ai_with_call("c2", "search", {"q": "x"}),
            ToolMessage(content="y" * 1000, tool_call_id="c2"),
        ]
        result, tokens_reduced = dedup_tool_outputs(messages)
        assert result[2].content == DEDUP_PLACEHOLDER
        assert result[4].content == "y" * 1000
        assert tokens_reduced == (1000 - len(DEDUP_PLACEHOLDER)) // 4

    def test_no_duplicates_returns_unchanged(self):
        messages = [
            HumanMessage(content="q"),
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="x" * 1000, tool_call_id="c1"),
            _ai_with_call("c2", "search", {"q": "different"}),
            ToolMessage(content="z" * 1000, tool_call_id="c2"),
        ]
        result, tokens_reduced = dedup_tool_outputs(messages)
        assert tokens_reduced == 0
        assert [m.content for m in result] == [m.content for m in messages]

    def test_empty_messages_list(self):
        assert dedup_tool_outputs([]) == ([], 0)

    def test_protected_tools_skipped(self):
        messages = [
            HumanMessage(content="q"),
            _ai_with_call("m1", "memory", {"q": "x"}),
            ToolMessage(content="x" * 1000, tool_call_id="m1"),
            _ai_with_call("m2", "memory", {"q": "x"}),
            ToolMessage(content="y" * 1000, tool_call_id="m2"),
        ]
        result, tokens_reduced = dedup_tool_outputs(messages, protected_tools={"memory"})
        assert tokens_reduced == 0
        assert result[2].content == "x" * 1000
        assert result[4].content == "y" * 1000

    def test_signature_grouping_by_name_and_args(self):
        # Same args but different tool names: not duplicates.
        diff_name = [
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="x" * 1000, tool_call_id="c1"),
            _ai_with_call("c2", "fetch", {"q": "x"}),
            ToolMessage(content="z" * 1000, tool_call_id="c2"),
        ]
        result, tokens_reduced = dedup_tool_outputs(diff_name)
        assert tokens_reduced == 0
        assert result[1].content == "x" * 1000
        assert result[3].content == "z" * 1000
        # Same name but different args: not duplicates.
        diff_args = [
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="x" * 1000, tool_call_id="c1"),
            _ai_with_call("c2", "search", {"q": "y"}),
            ToolMessage(content="z" * 1000, tool_call_id="c2"),
        ]
        result, tokens_reduced = dedup_tool_outputs(diff_args)
        assert tokens_reduced == 0
        assert result[1].content == "x" * 1000
        assert result[3].content == "z" * 1000

    def test_original_list_not_mutated(self):
        first_output = ToolMessage(content="x" * 1000, tool_call_id="c1")
        messages = [
            _ai_with_call("c1", "search", {"q": "x"}),
            first_output,
            _ai_with_call("c2", "search", {"q": "x"}),
            ToolMessage(content="y" * 1000, tool_call_id="c2"),
        ]
        result, _ = dedup_tool_outputs(messages)
        assert result is not messages
        assert messages[1].content == "x" * 1000
        assert result[1].content == DEDUP_PLACEHOLDER


# --- TestToolOutputPrune ---


class TestToolOutputPrune:
    def test_recent_outputs_within_protect_window_untouched(self):
        messages = [
            HumanMessage(content="q"),
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="p" * 100, tool_call_id="c1"),
        ]
        result, tokens_reduced = prune_tool_outputs(
            messages, protect_tokens=1000, min_reduction_tokens=50
        )
        assert tokens_reduced == 0
        assert result[2].content == "p" * 100

    def test_old_outputs_beyond_window_pruned_and_protected_skipped(self):
        messages = [
            HumanMessage(content="q"),
            _ai_with_call("m1", "memory", {"q": "x"}),
            ToolMessage(content="m" * 8000, tool_call_id="m1"),
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="p" * 8000, tool_call_id="c1"),
            HumanMessage(content="q2"),
        ]
        result, tokens_reduced = prune_tool_outputs(
            messages, protect_tokens=1000, min_reduction_tokens=100, protected_tools={"memory"}
        )
        # memory output is protected even though it is old; search output is pruned.
        assert result[2].content == "m" * 8000
        assert result[4].content == PRUNE_MARKER
        assert tokens_reduced == 8000 // 4

    def test_stops_at_summary_message(self):
        summary = HumanMessage(
            content="[CONTEXT COMPACTION]", additional_kwargs={"lc_source": "summarization"}
        )
        messages = [
            HumanMessage(content="q0"),
            _ai_with_call("c0", "search", {"q": "old"}),
            ToolMessage(content="o" * 8000, tool_call_id="c0"),
            summary,
            _ai_with_call("c1", "search", {"q": "new"}),
            ToolMessage(content="n" * 8000, tool_call_id="c1"),
        ]
        result, tokens_reduced = prune_tool_outputs(
            messages, protect_tokens=500, min_reduction_tokens=100
        )
        # Traversal stops at the summary: only the post-summary tool output is pruned.
        assert result[2].content == "o" * 8000
        assert result[5].content == PRUNE_MARKER
        assert tokens_reduced == 8000 // 4

    def test_min_reduction_tokens_not_met_returns_unchanged(self):
        messages = [
            HumanMessage(content="q"),
            _ai_with_call("c1", "search", {"q": "x"}),
            ToolMessage(content="p" * 8000, tool_call_id="c1"),
        ]
        result, tokens_reduced = prune_tool_outputs(
            messages, protect_tokens=10, min_reduction_tokens=100_000
        )
        assert tokens_reduced == 0
        assert result[2].content == "p" * 8000

    def test_empty_messages_list(self):
        assert prune_tool_outputs([], protect_tokens=10, min_reduction_tokens=1) == ([], 0)


# --- TestTargetTruncation ---


class TestTargetTruncation:
    def test_truncates_largest_first_until_target_met(self):
        big = ToolMessage(content="a" * 10000, tool_call_id="c1")
        mid = ToolMessage(content="b" * 8000, tool_call_id="c2")
        small = ToolMessage(content="c" * 6000, tool_call_id="c3")
        messages = [
            _ai_with_call("c1", "search", {"q": 1}),
            big,
            _ai_with_call("c2", "search", {"q": 2}),
            mid,
            _ai_with_call("c3", "search", {"q": 3}),
            small,
        ]
        # Budget equals exactly the reduction from truncating the largest output:
        # only "big" may be truncated; if a smaller one were chosen first the
        # budget would not be exhausted by "big" alone and it would stay intact.
        head = "a" * 600
        tail = "a" * 600
        expected = head + "...[truncated 8800 chars]..." + tail
        result, tokens_reduced = target_truncate_tool_outputs(
            messages, target_reduction_tokens=(10000 - len(expected)) // 4
        )
        assert result[1].content == expected
        assert result[3].content == "b" * 8000
        assert result[5].content == "c" * 6000
        assert tokens_reduced == (10000 - len(expected)) // 4

    def test_stops_at_target_reduction(self):
        big = ToolMessage(content="a" * 10000, tool_call_id="c1")
        mid = ToolMessage(content="b" * 8000, tool_call_id="c2")
        messages = [
            _ai_with_call("c1", "search", {"q": 1}),
            big,
            _ai_with_call("c2", "search", {"q": 2}),
            mid,
        ]
        huge_target = 100_000
        result, tokens_reduced = target_truncate_tool_outputs(
            messages,
            target_reduction_tokens=huge_target,
            min_output_chars=500,
            max_output_chars=2000,
        )
        # Both candidates truncated: head 600 + tail 600 + omission marker.
        assert result[1].content.startswith("a" * 600)
        assert result[1].content.endswith("a" * 600)
        assert result[3].content.startswith("b" * 600)
        assert result[3].content.endswith("b" * 600)
        assert tokens_reduced > 0

    def test_oversize_content_head_tail_format(self):
        oversize = ToolMessage(content="x" * 10000, tool_call_id="c1")
        messages = [
            _ai_with_call("c1", "search", {"q": "x"}),
            oversize,
        ]
        result, tokens_reduced = target_truncate_tool_outputs(
            messages,
            target_reduction_tokens=100_000,
            min_output_chars=500,
            max_output_chars=2000,
        )
        truncated = result[1].content
        assert truncated.startswith("x" * 600)
        assert truncated.endswith("x" * 600)
        assert "...[truncated 8800 chars]..." in truncated
        assert len(truncated) == 600 + len("...[truncated 8800 chars]...") + 600
        assert tokens_reduced == (10000 - len(truncated)) // 4

    def test_small_outputs_skipped_and_protected_skipped(self):
        tiny = ToolMessage(content="t" * 100, tool_call_id="c1")
        protected_big = ToolMessage(content="m" * 10000, tool_call_id="c2")
        messages = [
            _ai_with_call("c1", "search", {"q": 1}),
            tiny,
            _ai_with_call("c2", "memory", {"q": 2}),
            protected_big,
        ]
        result, tokens_reduced = target_truncate_tool_outputs(
            messages,
            target_reduction_tokens=100_000,
            min_output_chars=500,
            max_output_chars=2000,
            protected_tools={"memory"},
        )
        assert tokens_reduced == 0
        assert result[1].content == "t" * 100
        assert result[3].content == "m" * 10000

    def test_empty_messages_list(self):
        assert target_truncate_tool_outputs([], target_reduction_tokens=100) == ([], 0)
