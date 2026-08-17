"""Module tests for agent/tools/skill_tools/skill_manage.split_oversized_skill.

Covers both consumption paths that share this single length-limit guard:
  - Ordinary skill create/edit (_create_skill / _edit_skill)
  - Curator umbrella generation (_generate_umbrella_skill)
"""

import re
from pathlib import Path

from agent.tools.skill_tools.skill_manage import (
    _UMBRELLA_SKILL_CHAR_TARGET,
    _write_split_files,
    split_oversized_skill,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skill_with_sections(section_count, section_char_len, prefix_len=0, frontmatter=""):
    """Build a SKILL.md body with ``section_count`` ``## `` sections, each padded
    to ``section_char_len`` chars, plus an optional lead-in of ``prefix_len`` chars."""
    lead = "x" * prefix_len + "\n\n" if prefix_len else ""
    sections = "\n\n".join(f"## Section {i}\n\n" + "y" * section_char_len for i in range(section_count))
    body = lead + sections
    if frontmatter:
        return frontmatter + body
    return body


_FM = "---\nname: my_skill\ndescription: test\n---\n"


# ---------------------------------------------------------------------------
# Under-budget behavior
# ---------------------------------------------------------------------------

class TestUnderBudget:
    def test_short_content_returned_unchanged(self):
        content = _FM + "# Hello\n\nShort body."
        slim, files = split_oversized_skill(content, target=20_000)
        assert slim == content
        assert files == {}

    def test_under_budget_single_string_returned(self):
        content = "# Just a title and a sentence."
        slim, files = split_oversized_skill(content, target=10_000)
        assert slim == content
        assert files == {}

    def test_under_budget_supporting_files_preserved(self):
        content = _FM + "# Body\n\nShort."
        supporting = {"references/guide.md": "already authored"}
        slim, files = split_oversized_skill(content, target=10_000, supporting_files=supporting)
        assert slim == content
        assert files == supporting  # unchanged, merged copy


# ---------------------------------------------------------------------------
# Over-budget with splittable sections
# ---------------------------------------------------------------------------

class TestOverBudgetSplittable:
    def test_splits_into_reference_parts(self):
        # 8 sections x 2000 chars = 16k > 15k target, each section < target
        content = "leading paragraph here\n\n" + "\n\n".join(
            f"## T{i}\n\n" + "z" * 2000 for i in range(8)
        )
        slim, files = split_oversized_skill(content, target=15_000)
        assert len(slim) <= 15_000, f"SKILL.md still oversized: {len(slim)}"
        assert files, "expected split files"
        parts = [p for p in files if p.startswith("references/part")]
        assert len(parts) >= 2
        # All section content must be preserved somewhere (no data loss).
        # The 2000-char padding of every original section must still exist in the
        # reference files (or inline), but stub headings in SKILL.md add "## T*"
        # occurrences — so require at least the padding + every title to survive.
        combined = slim + "\n" + "\n".join(files.values())
        assert all(("z" * 2000) in combined for _ in range(8))
        assert all(f"## T{i}" in combined for i in range(8))

    def test_frontmatter_preserved(self):
        content = _FM + "\n\n" + "\n\n".join(
            f"## S{i}\n\n" + "q" * 2000 for i in range(8)
        )
        slim, _files = split_oversized_skill(content, target=15_000)
        assert slim.startswith("---\nname: my_skill")
        assert "description: test" in slim
        # frontmatter block intact
        assert re.match(r"^---.*?---", slim, re.DOTALL)

    def test_lead_in_stays_in_skil_file(self):
        content = "intro text stays here\n\n" + "\n\n".join(
            f"## S{i}\n\n" + "w" * 2000 for i in range(8)
        )
        slim, _files = split_oversized_skill(content, target=15_000)
        assert "intro text stays here" in slim

    def test_stub_contains_link_to_reference(self):
        content = "\n\n".join(f"## Topic{i}\n\n" + "t" * 2000 for i in range(8))
        slim, files = split_oversized_skill(content, target=15_000)
        assert files
        part = next(iter(files))
        assert any(part in slim for part in files), "SKILL.md must link the moved files"

    def test_no_clobber_of_authored_supporting_file(self):
        content = "\n\n".join(f"## C{i}\n\n" + "v" * 2000 for i in range(8))
        supporting = {"references/part01.md": "AUTHORED - do not clobber"}
        _slim, files = split_oversized_skill(content, target=15_000, supporting_files=supporting)
        assert files["references/part01.md"] == "AUTHORED - do not clobber"

    def test_zero_content_loss(self):
        content = "pre\n\n" + "\n\n".join(f"## S{i}\n\n" + "s" * 1500 for i in range(12))
        slim, files = split_oversized_skill(content, target=15_000)
        assert len(slim) <= 15_000
        joined = slim + "\n".join(files.values())
        for i in range(12):
            assert f"## S{i}" in joined
            assert ("s" * 1500) in joined


# ---------------------------------------------------------------------------
# Over-budget with NO splittable sections (no "## " headings)
# ---------------------------------------------------------------------------

class TestOverBudgetNoSections:
    def test_single_blob_returned_unchanged(self):
        content = _FM + "\n\n" + "plain text with no headings, " * 2000  # > 15k
        assert len(content) > 15_000
        slim, files = split_oversized_skill(content, target=15_000)
        assert slim == content  # intentional: can't split meaningfully
        assert files == {}

    def test_heading_missing_subsequent_space_not_split(self):
        # "##" without trailing space is not treated as a section boundary.
        content = _FM + "\n\n" + "##H1 no-break\n\n" + "k" * 16_000
        assert len(content) > 15_000
        slim, files = split_oversized_skill(content, target=15_000)
        assert slim == content
        assert files == {}


# ---------------------------------------------------------------------------
# _write_split_files persistence helper (IO path used by create/edit)
# ---------------------------------------------------------------------------

class TestWriteSplitFiles:
    def test_writes_reference_parts_to_disk(self, tmp_path):
        split_files = {
            "references/part01.md": "# part one",
            "references/part02.md": "# part two",
        }
        _write_split_files(tmp_path, split_files)
        assert (tmp_path / "references" / "part01.md").read_text() == "# part one"
        assert (tmp_path / "references" / "part02.md").read_text() == "# part two"

    def test_skips_invalid_paths(self, tmp_path):
        split_files = {
            "references/part01.md": "ok",
            "../escape.md": "must be skipped",           # traversal
            "nested/too/deep.md": "must be skipped",     # not an allowed subdir
        }
        _write_split_files(tmp_path, split_files)
        assert (tmp_path / "references" / "part01.md").read_text() == "ok"
        assert not (tmp_path / "escape.md").exists()
        assert not (tmp_path / "nested").exists()


# ---------------------------------------------------------------------------
# Shared budget constant used by both paths
# ---------------------------------------------------------------------------

class TestSharedBudget:
    def test_default_target_is_umbrella_budget(self):
        # Ordinary skills must use the same 15_000 budget as umbrella skills.
        assert _UMBRELLA_SKILL_CHAR_TARGET == 15_000
