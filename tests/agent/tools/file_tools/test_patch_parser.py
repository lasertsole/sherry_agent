"""Parser for the Codex-compatible multi-file patch format."""

from __future__ import annotations

import pytest

from agent.tools.file_tools.patch_parser import (
    PatchParseError,
    parse_patch,
    validate_patch_path,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _patch(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_parses_add_update_delete_and_move():
    operations = parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Add File: src/new.py",
            "+import os",
            "+print('hello')",
            "*** Update File: src/existing.py",
            "@@",
            " def old():",
            "-    return None",
            "+    return 'updated'",
            "*** Delete File: src/old.py",
            "*** Update File: src/moved.py",
            "*** Move to: src/renamed.py",
            "@@ def section",
            "-old",
            "+new",
            "*** End Patch",
        )
    )
    assert [op.kind for op in operations] == ["add", "update", "delete", "move"]
    assert operations[0].content_lines == ("import os", "print('hello')")
    assert operations[1].path == "src/existing.py"
    hunk = operations[1].hunks[0]
    assert hunk.old_text == "def old():\n    return None"
    assert hunk.new_text == "def old():\n    return 'updated'"
    assert operations[3].dest_path == "src/renamed.py"
    assert operations[3].hunks[0].heading == "def section"


def test_an_add_of_an_empty_file_is_valid():
    operations = parse_patch(_patch("*** Begin Patch", "*** Add File: empty.txt", "*** End Patch"))
    assert operations[0].content_lines == ()


def test_blank_lines_in_an_add_body_are_tolerated():
    operations = parse_patch(
        _patch("*** Begin Patch", "*** Add File: a.txt", "+one", "", "+three", "*** End Patch")
    )
    assert operations[0].content_lines == ("one", "", "three")


def test_a_move_only_update_is_valid():
    operations = parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Update File: old.txt",
            "*** Move to: new.txt",
            "*** End Patch",
        )
    )
    assert operations[0].kind == "move"
    assert operations[0].hunks == ()


def test_multiple_hunks_are_preserved():
    operations = parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Update File: a.py",
            "@@",
            "-one",
            "+ONE",
            "@@ second",
            "-two",
            "+TWO",
            "*** End Patch",
        )
    )
    assert len(operations[0].hunks) == 2
    assert operations[0].hunks[1].heading == "second"


def test_windows_separators_are_normalized():
    operations = parse_patch(
        _patch("*** Begin Patch", "*** Add File: src\\nested\\a.txt", "+x", "*** End Patch")
    )
    assert operations[0].path == "src/nested/a.txt"


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "C:/Windows/x", "c:\\temp\\x", "../escape.txt", "src/../../x", "/"],
)
def test_unsafe_paths_are_refused(bad):
    with pytest.raises(PatchParseError):
        validate_patch_path(bad, 1)


def test_missing_begin_marker_is_refused():
    with pytest.raises(PatchParseError, match="must start"):
        parse_patch("*** Add File: a.txt\n+x\n*** End Patch")


def test_missing_end_marker_is_refused():
    with pytest.raises(PatchParseError, match="missing"):
        parse_patch("*** Begin Patch\n*** Add File: a.txt\n+x\n")


def test_add_body_requires_a_plus_prefix():
    with pytest.raises(PatchParseError, match="must start with"):
        parse_patch("*** Begin Patch\n*** Add File: a.txt\nplain line\n*** End Patch\n")


def test_update_requires_hunks():
    with pytest.raises(PatchParseError, match="no hunks"):
        parse_patch("*** Begin Patch\n*** Update File: a.txt\n*** End Patch\n")


def test_move_outside_an_update_is_refused():
    with pytest.raises(PatchParseError, match="must follow"):
        parse_patch("*** Begin Patch\n*** Move to: b.txt\n*** End Patch\n")


def test_content_after_end_is_refused():
    with pytest.raises(PatchParseError, match="after"):
        parse_patch("*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch\ntrailing\n")


def test_unknown_header_is_refused():
    with pytest.raises(PatchParseError, match="unexpected line"):
        parse_patch("*** Begin Patch\n*** Rename File: a.txt\n*** End Patch\n")


def test_an_empty_patch_is_refused():
    with pytest.raises(PatchParseError, match="no operations"):
        parse_patch("*** Begin Patch\n*** End Patch\n")


def test_a_hunk_line_with_a_bad_prefix_is_refused():
    with pytest.raises(PatchParseError, match="hunk lines"):
        parse_patch(
            "*** Begin Patch\n*** Update File: a.txt\n@@\n?not-a-hunk-line\n*** End Patch\n"
        )
