"""``write_file``'s JSON result with the inline diff preview.

The success return changed from the plain ``File written successfully …``
string to JSON (2026-10-24) so the client can render the edit as a colored
diff. Pinned here:

* overwriting an existing text file answers a unified diff of old→new
  (byte-equal to ``difflib.unified_diff`` of the same inputs);
* a creation answers ``isNew: true`` and ``diff: null`` (nothing to diff
  against — the client shows an all-added preview from the write's own text);
* append answers a diff whose added side is exactly the appended tail;
* appending to a nonexistent file is REFUSED (the encoding probe needs an existing file);
* an existing-but-EMPTY file is NOT a creation (``isNew: false``);
* the error branches keep their shape and carry no ``diff`` key — the old
  failure contract is untouched;
* a binary (non-UTF-8) target still refuses with its own error, and a diff is
  never attempted against one.
"""

import difflib
import json

import pytest

from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils
from agent.tools.pub_base import read_state

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-write-diff"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A session root at tmp_path with the license registry emptied."""
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    read_state.forget_all()
    yield root
    read_state.forget_all()


def _write(path: str, text: str, **kwargs) -> dict:
    raw = build_write_file_tool()._core(path, text, session_id=SESSION, **kwargs)
    return json.loads(raw)


def _read(path: str) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=SESSION))


def test_overwrite_answers_a_real_unified_diff(project):
    target = project / "notes.txt"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    _read("notes.txt")  # license the overwrite
    new_text = "line one\nline TWO\nline three\nline four\n"

    result = _write("notes.txt", new_text)

    assert result["success"] is True
    assert result["isNew"] is False
    assert result["append"] is False
    expected = "".join(
        difflib.unified_diff(
            "line one\nline two\nline three\n".splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{result['path']}",
            tofile=f"b/{result['path']}",
        )
    )
    assert result["diff"] == expected
    # The write actually happened.
    assert target.read_text(encoding="utf-8") == new_text


def test_creation_answers_is_new_and_no_diff(project):
    result = _write("fresh.txt", "hello\n")

    assert result["success"] is True
    assert result["isNew"] is True
    assert result["diff"] is None
    assert (project / "fresh.txt").read_text(encoding="utf-8") == "hello\n"


def test_append_diff_covers_exactly_the_tail(project):
    target = project / "log.txt"
    target.write_text("first\n", encoding="utf-8")
    _read("log.txt")

    result = _write("log.txt", "second\nthird\n", append=True)

    assert result["success"] is True
    assert result["append"] is True
    assert result["isNew"] is False
    added = [
        line[1:]
        for line in (result["diff"] or "").split("\n")
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert added == ["second", "third"]
    removed = [
        line[1:]
        for line in (result["diff"] or "").split("\n")
        if line.startswith("-") and not line.startswith("---")
    ]
    assert removed == []


def test_append_to_a_nonexistent_file_is_refused(project):
    # The tool detects the target's encoding before appending; an absent file
    # has none, so append requires an existing file (pre-existing behavior —
    # the plan's "append creates" premise was wrong and is corrected here).
    result = _write("new-log.txt", "entry\n", append=True)

    assert "error" in result
    assert "diff" not in result
    assert not (project / "new-log.txt").exists()


def test_an_existing_empty_file_is_not_a_creation(project):
    target = project / "empty.txt"
    target.write_text("", encoding="utf-8")
    _read("empty.txt")

    result = _write("empty.txt", "now filled\n")

    assert result["isNew"] is False
    assert result["diff"] is not None
    assert "now filled" in result["diff"]


def test_error_branches_keep_their_shape_without_a_diff_key(project):
    # Overwriting an unread file is refused BEFORE any diff work, and the
    # refusal is the exact old JSON shape (no "diff", no "isNew").
    target = project / "unseen.txt"
    target.write_text("secret\n", encoding="utf-8")

    result = _write("unseen.txt", "clobber\n")

    assert "error" in result
    assert "diff" not in result
    assert target.read_text(encoding="utf-8") == "secret\n"


def test_binary_targets_still_refuse_without_a_diff(project):
    target = project / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\xff")
    _read("blob.bin")

    result = _write("blob.bin", "text\n")

    assert "error" in result
    assert "diff" not in result
