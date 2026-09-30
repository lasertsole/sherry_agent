"""Inline chain-of-thought is moved from the answer channel to the reasoning one (B4).

The scrubber's real contract is a *split-invariance*: the same token stream
chopped into different chunks must produce the same visible text and the same
recovered reasoning, because the provider decides where the chunk boundaries fall
and a tag can be split anywhere (``"<thi"`` / ``"nk>…</thin"`` / ``"k>"``). A
whole-message regex passes the single-chunk case and leaks the thinking in the
streaming one, so most of this file re-runs one corpus at every cut point.

The second half is the redirect: what came out of a block must be *recoverable*
(``take_reasoning``), not just absent from the answer — dropping it would fix the
leak by emptying the client's thinking block.
"""

from __future__ import annotations

import re

import pytest

from agent.security.think_scrub import (
    INLINE_REASONING_TAGS,
    StreamingReasoningScrubber,
    strip_inline_reasoning,
)

pytestmark = [pytest.mark.unit]

CORPUS = [
    "plain answer",
    "",
    "<think>secret</think>visible",
    "before <think>secret</think> after",
    "<thinking>deep</thinking>a",
    "a<reasoning>r</reasoning>b",
    "<think>unterminated secret",
    "text <thi",
    "an answer with < meaning",
    "<think>x</think>",
    "<THINK>upper</THINK>visible",
    "compare < thinking > spaced",
    "a < b > c",
    "<think>a</think>mid<think>b</think>end",
    "1 < 2 and <thing> is a word",
    "use <thoughts> tags",
]


@pytest.mark.parametrize("text", CORPUS)
def test_one_chunk_matches_the_whole_text_helper(text):
    assert strip_inline_reasoning(text) == _visible_of(text)


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 1000])
def test_visible_text_is_invariant_to_the_chunk_boundaries(text, chunk_size):
    """The property the streaming design exists for: any split, same output."""
    scrubber = StreamingReasoningScrubber()
    out = "".join(scrubber.feed(text[i : i + chunk_size]) for i in range(0, len(text), chunk_size))
    out += scrubber.flush()

    assert out == _visible_of(text)


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 1000])
def test_recovered_reasoning_is_invariant_to_the_chunk_boundaries(text, chunk_size):
    """Whatever left the answer channel must come back on the reasoning one."""
    scrubber = StreamingReasoningScrubber()
    recovered = ""
    for i in range(0, len(text), chunk_size):
        scrubber.feed(text[i : i + chunk_size])
        recovered += scrubber.take_reasoning()
    scrubber.flush()
    recovered += scrubber.take_reasoning()

    assert recovered == _reasoning_of(text)


def _visible_of(text: str) -> str:
    """The non-streaming reference: one whole-message regex over the full text.

    A different algorithm on purpose — the streaming scrubber must agree with it
    at every cut point. An unterminated block runs to the end of the text, which
    is what the scrubber's ``flush`` closes too.
    """
    tags = "|".join(INLINE_REASONING_TAGS)
    return re.sub(rf"<(?:{tags})>.*?(?:</(?:{tags})>|\Z)", "", text, flags=re.DOTALL | re.I)


def _reasoning_of(text: str) -> str:
    """The reference extraction: the body of every block, unterminated one included."""
    tags = "|".join(INLINE_REASONING_TAGS)
    return "".join(
        m.group(1)
        for m in re.finditer(
            rf"<(?:{tags})>(.*?)(?:</(?:{tags})>|\Z)", text, flags=re.DOTALL | re.I
        )
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text", "expected"),
    [
        ("single-block", "<think>cot</think>answer", "answer"),
        ("leading-and-trailing-text", "a<think>cot</think>b", "ab"),
        ("two-blocks", "<think>1</think>x<reasoning>2</reasoning>y", "xy"),
        ("block-only", "<thinking>cot</thinking>", ""),
        ("unterminated-block", "answer<think>cot never closes", "answer"),
        ("case-insensitive", "<Think>cot</Think>answer", "answer"),
        ("uppercase-close", "<think>cot</THINK>answer", "answer"),
        (
            "other-tags-untouched",
            "use <thought> and <thing> tags",
            "use <thought> and <thing> tags",
        ),
        ("less-than-in-prose", "if a < b then c", "if a < b then c"),
    ],
)
def test_shapes(label, text, expected):
    assert strip_inline_reasoning(text) == expected, label


# ---------------------------------------------------------------------------
# Redirect: the recovered text is handed over, not discarded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text", "expected"),
    [
        ("single-block", "<think>cot</think>answer", "cot"),
        ("two-blocks", "<think>1</think>x<reasoning>2</reasoning>y", "12"),
        ("unterminated-block", "answer<think>cot never closes", "cot never closes"),
        ("block-then-text", "a<thinking>deep</thinking>b", "deep"),
        ("no-block", "just an answer", ""),
        ("other-tags-untouched", "use <thought> and <thing> tags", ""),
    ],
)
def test_recovered_reasoning(label, text, expected):
    assert strip_reasoning_capture(text) == expected, label


def strip_reasoning_capture(text: str) -> str:
    scrubber = StreamingReasoningScrubber()
    scrubber.feed(text)
    return scrubber.take_reasoning()


def test_reasoning_is_recovered_while_the_block_is_still_open():
    """Not only on the close tag: a long CoT streams out as it arrives."""
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("<think>step one. ") == ""
    assert scrubber.take_reasoning() == "step one. "
    assert scrubber.feed("step two.") == ""
    assert scrubber.take_reasoning() == "step two."


def test_take_reasoning_drains():
    scrubber = StreamingReasoningScrubber()
    scrubber.feed("<think>cot</think>")

    assert scrubber.take_reasoning() == "cot"
    assert scrubber.take_reasoning() == ""


def test_an_unterminated_block_hands_its_held_fragment_over_too():
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("<think>cot</thi") == ""
    assert scrubber.flush() == ""
    assert scrubber.take_reasoning() == "cot</thi"


def test_reasoning_recovered_is_never_also_visible():
    """The two channels partition the input — no text is duplicated or lost."""
    text = "Answer one. <think>why?</think> Answer two. <reasoning>more</reasoning>"
    scrubber = StreamingReasoningScrubber()
    visible = scrubber.feed(text) + scrubber.flush()
    reasoning = scrubber.take_reasoning()

    assert visible == "Answer one.  Answer two. "
    assert reasoning == "why?more"


def test_a_held_back_fragment_is_flushed_as_text_when_the_stream_ends():
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("answer <thi") == "answer "
    assert scrubber.flush() == "<thi"
    assert scrubber.take_reasoning() == ""


def test_a_completed_tag_across_the_flush_boundary_is_resolved_before_flush():
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("<thi") == ""
    assert scrubber.feed("nk>cot</think>ok") == "ok"
    assert scrubber.flush() == ""
    assert scrubber.take_reasoning() == "cot"


def test_text_inside_an_open_block_is_never_emitted():
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("<think>") == ""
    assert scrubber.feed("hidden reasoning") == ""
    assert scrubber.feed("more</think>") == ""
    assert scrubber.feed("shown") == "shown"
    assert scrubber.flush() == ""
    assert scrubber.take_reasoning() == "hidden reasoningmore"


def test_a_fragment_that_could_not_start_a_tag_is_emitted_immediately():
    """``<x`` cannot become ``<think>``, so holding it back would only delay it."""
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("<x") == "<x"


def test_feed_of_an_empty_chunk_is_empty():
    scrubber = StreamingReasoningScrubber()

    assert scrubber.feed("") == ""
    assert scrubber.flush() == ""
    assert scrubber.take_reasoning() == ""


def test_the_tag_set_is_the_one_the_repetition_guard_extracts_with():
    """The two must agree on what "the reasoning stream" means."""
    from agent.middlewares.output_repetition_guard import repetition_detectors

    source = " ".join(p.pattern for p in repetition_detectors._THINK_PATTERNS)
    for tag in INLINE_REASONING_TAGS:
        assert f"<{tag}>" in source, tag
