"""Project file browser tuning (read-only tree + preview endpoints)."""

from typing import TypedDict


class FileBrowserConfig(TypedDict):
    """Bounds for ``GET /project/tree`` and ``GET /project/file``."""

    #: Largest file the preview endpoint will return (413 above it). The agent's
    #: read_file has no such cap; a UI preview needs one.
    max_read_bytes: int
    #: Entries returned per directory level (the truncation guard for trees like
    #: node_modules, which can hold tens of thousands of names).
    max_entries_per_level: int
    #: Directory names never walked.
    skip_dirs: frozenset[str]
    #: File suffixes never listed.
    skip_suffixes: frozenset[str]
    #: Deepest relative path the tree endpoint will serve (a lazy tree still
    #: needs a floor against a symlink-free but absurdly deep project).
    max_tree_depth: int


FILE_BROWSER: FileBrowserConfig = {
    "max_read_bytes": 1 * 1024 * 1024,
    "max_entries_per_level": 500,
    "skip_dirs": frozenset(
        {
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "dist",
            "build",
            "target",
            ".next",
            ".nuxt",
            ".turbo",
        }
    ),
    "skip_suffixes": frozenset({".pyc", ".pyo", ".so", ".o", ".a", ".lock"}),
    "max_tree_depth": 24,
}
