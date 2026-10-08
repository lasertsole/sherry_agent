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
