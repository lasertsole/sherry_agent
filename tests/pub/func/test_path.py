"""Unit tests for pub/func/path.py: has_traversal_component + validate_within_dir."""

from pathlib import Path

import pytest

from pub.func.path import has_traversal_component, validate_within_dir

pytestmark = [pytest.mark.unit]


class TestHasTraversalComponent:
    @pytest.mark.parametrize(
        "path_str",
        [
            "..",
            "a/..",
            "../secret",
            "../../etc/passwd",
            "a/../b",
            "%2e%2e/secret",
            "%2E%2E/secret",
            "..%2fsecret",
            "..%5csecret",
            "a%2F..%2Fb",
            "foo\\..\\bar",
            "...",
            "....",
        ],
    )
    def test_rejects_traversal_forms(self, path_str):
        assert has_traversal_component(path_str) is True

    @pytest.mark.parametrize(
        "path_str",
        [
            "",
            "src/main.py",
            "foo..bar",
            "配置/文件.md",
            "a.b.c",
            "notes/2024..2025.md",
        ],
    )
    def test_allows_regular_names(self, path_str):
        assert has_traversal_component(path_str) is False


class TestValidateWithinDir:
    def test_path_inside_root_passes(self, tmp_path):
        (tmp_path / "sub").mkdir()

        assert validate_within_dir(tmp_path / "sub" / "f.txt", tmp_path) is None

    def test_path_outside_root_returns_error(self, tmp_path):
        assert validate_within_dir(Path("/etc/passwd"), tmp_path) is not None
