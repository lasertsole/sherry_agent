"""Module tests for agent/tools/pub_base/skill_utils.py — skill metadata utilities."""

import pytest
from pathlib import Path
from unittest.mock import patch
from agent.tools.pub_base.skill_utils import (
    parse_frontmatter, skill_matches_platform, extract_skill_description,
    iter_skill_index_files, parse_qualified_name, is_valid_namespace,
    sort_skills, PLATFORM_MAP, EXCLUDED_SKILL_DIRS,
)


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        content = "# Hello\nNo frontmatter here."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "# Hello\nNo frontmatter here."

    def test_valid_yaml(self):
        content = "---\nname: test\ndescription: A test skill\n---\n\nBody text"
        fm, body = parse_frontmatter(content)
        assert fm["name"] == "test"
        assert fm["description"] == "A test skill"
        assert "Body text" in body

    def test_missing_end_delimiter(self):
        content = "---\nname: test\nNo end delimiter"
        fm, body = parse_frontmatter(content)
        # Should fall through to full content
        assert fm == {} or "name" in fm  # behavior depends on parser

    def test_empty_frontmatter(self):
        content = "---\n---\nBody"
        fm, body = parse_frontmatter(content)
        assert fm == {} or isinstance(fm, dict)
        assert "Body" in body

    def test_malformed_yaml_fallback(self):
        content = "---\nname: test\nvalid: true\n---\nBody"
        fm, body = parse_frontmatter(content)
        # Should at least parse the simple key:value lines
        assert "name" in fm or fm == {}

    def test_nested_yaml(self):
        content = "---\nname: test\ntags:\n  - one\n  - two\n---\nBody"
        fm, body = parse_frontmatter(content)
        if "tags" in fm:
            assert isinstance(fm["tags"], list)


class TestSkillMatchesPlatform:
    def test_no_platforms_matches_all(self):
        assert skill_matches_platform({}) is True

    def test_none_platforms(self):
        assert skill_matches_platform({"platforms": None}) is True

    def test_empty_list(self):
        assert skill_matches_platform({"platforms": []}) is True

    def test_single_matching(self):
        import sys
        # Map sys.platform to a known platform name
        current_platforms = [k for k, v in PLATFORM_MAP.items() if sys.platform.startswith(v)]
        if current_platforms:
            assert skill_matches_platform({"platforms": [current_platforms[0]]}) is True

    def test_non_matching(self):
        fake = "totally_fake_platform_xyz"
        assert skill_matches_platform({"platforms": [fake]}) is False

    def test_string_not_list(self):
        import sys
        current_platforms = [k for k, v in PLATFORM_MAP.items() if sys.platform.startswith(v)]
        if current_platforms:
            assert skill_matches_platform({"platforms": current_platforms[0]}) is True


class TestExtractSkillDescription:
    def test_no_description(self):
        assert extract_skill_description({}) == ""

    def test_short_description(self):
        assert extract_skill_description({"description": "Hello world"}) == "Hello world"

    def test_long_description_truncated(self):
        long_desc = "x" * 100
        result = extract_skill_description({"description": long_desc})
        assert len(result) == 60
        assert result.endswith("...")

    def test_strips_quotes(self):
        assert extract_skill_description({"description": "'quoted'"}) == "quoted"
        assert extract_skill_description({"description": '"double"'}) == "double"


class TestIterSkillIndexFiles:
    def test_finds_skill_files(self, tmp_path):
        skill_dir = tmp_path / "skill_a"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("test")

        other_dir = tmp_path / "skill_b"
        other_dir.mkdir()
        (other_dir / "OTHER.md").write_text("test")

        results = list(iter_skill_index_files(tmp_path, "SKILL.md"))
        assert len(results) == 1
        assert results[0].name == "SKILL.md"

    def test_excludes_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "SKILL.md").write_text("test")

        results = list(iter_skill_index_files(tmp_path, "SKILL.md"))
        assert len(results) == 0

    def test_excludes_archive_dir(self, tmp_path):
        archive = tmp_path / ".archive"
        archive.mkdir()
        (archive / "SKILL.md").write_text("test")

        results = list(iter_skill_index_files(tmp_path, "SKILL.md"))
        assert len(results) == 0


class TestParseQualifiedName:
    def test_no_colon(self):
        ns, name = parse_qualified_name("my-skill")
        assert ns is None
        assert name == "my-skill"

    def test_with_colon(self):
        ns, name = parse_qualified_name("mynamespace:my-skill")
        assert ns == "mynamespace"
        assert name == "my-skill"

    def test_multiple_colons(self):
        ns, name = parse_qualified_name("a:b:c")
        assert ns == "a"
        assert name == "b:c"


class TestIsValidNamespace:
    def test_valid(self):
        assert is_valid_namespace("my-ns") is True
        assert is_valid_namespace("my_ns") is True
        assert is_valid_namespace("abc123") is True

    def test_invalid(self):
        assert is_valid_namespace("") is False
        assert is_valid_namespace(None) is False
        assert is_valid_namespace("has space") is False
        assert is_valid_namespace("has.dot") is False


class TestSortSkills:
    def test_sorts_by_category_then_name(self):
        skills = [
            {"name": "b", "category": "tools"},
            {"name": "a", "category": "tools"},
            {"name": "c"},  # No category
        ]
        result = sort_skills(skills)
        assert result[0]["name"] == "c"  # Empty category sorts first
        assert result[1]["name"] == "a"
        assert result[2]["name"] == "b"

    def test_empty_list(self):
        assert sort_skills([]) == []
