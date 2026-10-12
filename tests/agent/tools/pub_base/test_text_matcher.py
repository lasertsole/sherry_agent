"""Behavioural tests for the fuzzy find-and-replace engine.

This module drives every ``patch_file`` / ``write_file`` edit, and it carried no
tests at all. The contract under test is the strategy ladder: the FIRST strategy
that matches wins, a non-exact match is re-indented to the file's own
indentation, an ambiguous match is refused unless ``replace_all`` is set, and a
backslash-escaped payload that does not match the file region is reported as
escape drift instead of silently replacing the wrong text.
"""

from __future__ import annotations

import pytest

from agent.tools.pub_base.text_matcher import (
    _strip_line_number_prefix,
    find_closest_lines,
    format_no_match_hint,
    fuzzy_find_and_replace,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Guards that run before any strategy
# ---------------------------------------------------------------------------


def test_empty_old_string_is_refused():
    out, count, strategy, error = fuzzy_find_and_replace("body\n", "", "x")

    assert (out, count, strategy) == ("body\n", 0, None)
    assert error == "old_string cannot be empty"


def test_identical_strings_are_refused():
    out, count, strategy, error = fuzzy_find_and_replace("body\n", "body", "body")

    assert (out, count, strategy) == ("body\n", 0, None)
    assert error == "old_string and new_string are identical"


def test_an_absent_pattern_reports_no_match():
    out, count, strategy, error = fuzzy_find_and_replace("alpha\n", "omega", "x")

    assert out == "alpha\n" and count == 0 and strategy is None
    assert error == "Could not find a match for old_string in the file"


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_exact_match_wins_and_reports_its_strategy():
    out, count, strategy, error = fuzzy_find_and_replace("a\nb\nc\n", "b", "B")

    assert (out, count, strategy, error) == ("a\nB\nc\n", 1, "exact", None)


def test_ambiguous_match_is_refused_until_replace_all():
    content = "x = 1\ny = 1\n"

    out, count, _strategy, error = fuzzy_find_and_replace(content, "= 1", "= 2")
    assert out == content and count == 0
    assert error is not None and "Found 2 matches" in error and "replace_all=True" in error

    out, count, strategy, error = fuzzy_find_and_replace(content, "= 1", "= 2", replace_all=True)
    assert (out, count, strategy, error) == ("x = 2\ny = 2\n", 2, "exact", None)


def test_a_differently_indented_block_is_reindented_to_the_file():
    # The file indents the block; old_string does not. line_trimmed comes before
    # indentation_flexible in the ladder, so it is what catches this shape — and
    # any non-exact strategy re-indents the insertion.
    content = "def f():\n    if ready:\n        run()\n"
    old = "if ready:\nrun()"
    new = "if ready:\n  run()\n  log()"

    out, count, strategy, error = fuzzy_find_and_replace(content, old, new)

    assert error is None and count == 1
    assert strategy == "line_trimmed", strategy
    # The replacement inherits the file's indentation, line by line.
    assert out == "def f():\n    if ready:\n      run()\n      log()\n"


def test_whitespace_normalized_match_keeps_the_files_bytes():
    content = "value\t=\tcompute()\n"
    out, count, strategy, error = fuzzy_find_and_replace(content, "value = compute()", "other()")

    assert error is None and count == 1 and strategy == "whitespace_normalized", strategy
    assert out == "other()\n"


def test_escape_normalized_match_accepts_a_backslash_escaped_pattern():
    # old_string carries a literal \n; the file has a real newline.
    out, count, strategy, error = fuzzy_find_and_replace("a\nb\n", "a\\nb", "c")

    assert error is None and count == 1 and strategy == "escape_normalized", strategy
    assert out == "c\n"


def test_trimmed_boundary_match_ignores_trailing_spaces():
    content = "start\nmiddle   \nend\n"
    out, count, strategy, error = fuzzy_find_and_replace(content, "middle", "MIDDLE")

    assert error is None and count == 1
    assert "MIDDLE" in out


def test_unicode_normalized_match_maps_typographic_characters():
    # The file carries curly quotes / an em dash; old_string uses ASCII.
    content = 'title = \u201cdraft\u201d\nnote = "x \u2014 y"\n'
    out, count, strategy, error = fuzzy_find_and_replace(content, 'title = "draft"', "title = 1")

    assert error is None and count == 1 and strategy == "unicode_normalized", strategy
    assert out.startswith("title = 1\n")


def test_escape_drift_is_reported_instead_of_replacing():
    # Both strings carry \" but the file region has no backslash: the caller
    # escaped a payload that is not JSON, so the match would be a guess.
    content = 'label = "plain"\n'
    out, count, _strategy, error = fuzzy_find_and_replace(
        content, 'label = \\"plain\\"', 'label = \\"changed\\"'
    )

    assert count == 0 and out == content
    assert error is not None and "Escape-drift detected" in error and "Re-read the file" in error


def test_replacement_uses_the_last_match_first():
    """Two matches replaced without replace_all=False ambiguity via replace_all."""
    content = "a\nbb\na\nbb\n"
    out, count, _strategy, error = fuzzy_find_and_replace(content, "bb", "cc", replace_all=True)

    assert error is None and count == 2
    assert out == "a\ncc\na\ncc\n"


# ---------------------------------------------------------------------------
# Failure hints
# ---------------------------------------------------------------------------


def test_closest_lines_suggest_near_misses():
    content = "def alpha():\n    pass\n\ndef beta():\n    pass\n"

    closest = find_closest_lines("def alphi():", content)

    assert closest, "a one-character miss must suggest something"
    # A numbered snippet of the closest region, not the pattern echoed back.
    assert "alpha" in closest and "def alphi():" not in closest, closest


def test_no_match_hint_is_built_for_a_close_miss():
    content = "def alpha():\n    pass\n"

    hint = format_no_match_hint(
        "Could not find a match for old_string in the file", 0, "def alphi():", content
    )

    assert "Did you mean" in hint
    assert "alpha" in hint


def test_no_match_hint_stays_quiet_without_a_close_line():
    hint = format_no_match_hint(
        "Could not find a match for old_string in the file", 0, "zzzzzz", "short\n"
    )

    assert "Did you mean" not in hint


def test_no_match_hint_ignores_other_errors():
    # An ambiguity/empty-pattern error carries its own advice already.
    assert format_no_match_hint("old_string cannot be empty", 0, "", "") == ""
    assert format_no_match_hint("Found 2 matches for old_string.", 0, "x", "x\nx\n") == ""


# ---------------------------------------------------------------------------
# Gap 1 — a copied snippet carries its line numbers
# ---------------------------------------------------------------------------


def test_line_number_prefixes_are_stripped_before_matching():
    content = "def foo() {\n    return bar;\n}\n"
    old = "10|def foo() {\n11|    return bar;\n12|}"

    out, count, strategy, error = fuzzy_find_and_replace(
        content, old, "def foo() {\n    return 1;\n}"
    )

    assert error is None and count == 1 and strategy == "line_number_prefix_stripped", strategy
    assert out == "def foo() {\n    return 1;\n}\n"


@pytest.mark.parametrize(
    "numbered",
    [
        "10|def foo() {\n11|    return bar;\n12|}",
        "10\tdef foo() {\n11\t    return bar;\n12\t}",
        "10: def foo() {\n11:     return bar;\n12: }",
    ],
)
def test_every_supported_line_number_format_strips(numbered):
    content = "def foo() {\n    return bar;\n}\n"

    _out, count, strategy, error = fuzzy_find_and_replace(content, numbered, "x")

    assert error is None and count == 1 and strategy == "line_number_prefix_stripped"


def test_one_unprefixed_line_disables_the_strip():
    # The block is not a numbered paste: the strip strategy must stay out of the
    # way, and whatever the ladder does next is some OTHER strategy's business.
    content = "def foo() {\n    return bar;\n}\n"
    old = "10|def foo() {\n    return bar;\n}"

    assert _strip_line_number_prefix(old) is None
    _out, _count, strategy, _error = fuzzy_find_and_replace(content, old, "x")

    assert strategy != "line_number_prefix_stripped"


def test_timestamps_are_not_line_numbers():
    # "12:" is not a line prefix without the space, so the strip strategy must
    # not treat a log excerpt as line-numbered text.
    assert _strip_line_number_prefix("12:34:56 error\n12:34:57 warn\n") is None

    _out, _count, strategy, _error = fuzzy_find_and_replace(
        "34:56 error\n", "12:34:56 error", "boom"
    )

    assert strategy != "line_number_prefix_stripped"


def test_a_blank_line_does_not_break_the_strip():
    content = "first\n\nsecond\n"

    out, count, strategy, error = fuzzy_find_and_replace(
        content, "1|first\n2|\n3|second\n", "gone\n"
    )

    assert error is None and count == 1 and strategy == "line_number_prefix_stripped", strategy
    assert out == "gone\n"


# ---------------------------------------------------------------------------
# Gap 2 — \uXXXX escapes written as literal characters
# ---------------------------------------------------------------------------


def test_unicode_escapes_are_unescaped_before_matching():
    content = "note = 'it\u2019s fine'\n"

    out, count, strategy, error = fuzzy_find_and_replace(
        content, "note = 'it\\u2019s fine'", "note = ok"
    )

    assert error is None and count == 1 and strategy == "unicode_escape_normalized", strategy
    assert out == "note = ok\n"


def test_unicode_escape_of_a_cjk_character():
    content = "name = \u4e2d\u6587\n"

    out, count, strategy, error = fuzzy_find_and_replace(
        content, "name = \\u4e2d\\u6587", "name = en"
    )

    assert error is None and count == 1 and strategy == "unicode_escape_normalized"
    assert out == "name = en\n"


def test_a_pattern_without_escapes_does_not_trigger_the_unescape():
    _out, count, strategy, _error = fuzzy_find_and_replace("plain\n", "plain\n", "other\n")

    assert count == 1 and strategy == "exact"  # the ladder never claims the unescape


# ---------------------------------------------------------------------------
# Gap 3 — replace_all accepts only precise matches
# ---------------------------------------------------------------------------


def test_replace_all_refuses_a_fuzzy_pattern_instead_of_bulk_editing_lookalikes():
    content = "x\t= 1\nx\t= 1\n"

    out, count, strategy, error = fuzzy_find_and_replace(
        content, "x = 1", "x = 2", replace_all=True
    )

    assert out == content and count == 0 and strategy is None
    assert error is not None and error.startswith("Could not find a precise match")
    assert "replace_all=True" in error and "fuzzy match(es) exist" in error


def test_the_same_fuzzy_pattern_still_works_without_replace_all():
    # The skip is specific to replace_all: a single fuzzy match is still applied.
    out, count, strategy, error = fuzzy_find_and_replace("x\t= 1\n", "x = 1", "x = 2")

    assert error is None and count == 1 and strategy == "whitespace_normalized"
    assert out == "x = 2\n"


def test_replace_all_still_runs_the_precise_strategies():
    # The strip is a precise strategy, so replace_all keeps it in play.
    content = "alpha\nbeta\ngamma\n"

    out, count, strategy, error = fuzzy_find_and_replace(
        content, "1|alpha\n2|beta\n3|gamma\n", "gone\n", replace_all=True
    )

    assert error is None and count == 1 and strategy == "line_number_prefix_stripped"
    assert out == "gone\n"


def test_replace_all_on_an_unmatched_pattern_reports_the_plain_miss():
    # No fuzzy match exists either: the ordinary no-match error is the honest one.
    _out, count, _strategy, error = fuzzy_find_and_replace("alpha\n", "zzz", "x", replace_all=True)

    assert count == 0 and error == "Could not find a match for old_string in the file"


# ---------------------------------------------------------------------------
# Gap 4 — the replacement keeps the file's quote style
# ---------------------------------------------------------------------------


def test_a_curly_quote_file_gets_curly_quotes_back():
    content = "Title = \u201cfirst draft\u201d\n"

    out, count, _strategy, error = fuzzy_find_and_replace(
        content, 'Title = "first draft"', 'Title = "second"'
    )

    assert error is None and count == 1
    assert out == "Title = \u201csecond\u201d\n"


def test_an_apostrophe_becomes_a_right_single_quote():
    content = "label = \u2018draft\u2019\n"

    out, _count, _strategy, error = fuzzy_find_and_replace(
        content, "label = 'draft'", "label = 'it's ok'"
    )

    assert error is None
    assert out == "label = \u2018it\u2019s ok\u2019\n"


def test_a_region_with_straight_quotes_is_left_alone():
    # A mixed region (a curly dash beside code quotes) must NOT have its code
    # quotes rewritten — that would corrupt the file.
    content = 'note = "x \u2014 y"\n'

    out, count, _strategy, error = fuzzy_find_and_replace(content, 'note = "x - y"', 'note = "z"')

    assert error is None and count == 1
    assert out == 'note = "z"\n'


def test_an_exact_match_keeps_the_new_string_verbatim():
    # The style pass rides with the fuzzy strategies only: a byte-exact match
    # means the caller already spelled the replacement itself.
    content = "\u201cdraft\u201d end\n"

    out, count, strategy, error = fuzzy_find_and_replace(content, "\u201cdraft\u201d", '"x"')

    assert error is None and count == 1 and strategy == "exact"
    assert out == '"x" end\n'
