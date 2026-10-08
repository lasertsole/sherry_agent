"""Directory names every isolation step skips.

One leaf module (no imports from the package) so the manifest walk in
``tree.py``, the merge in ``merge.py`` and the materialization in
``materialize.py`` share the set without an import cycle: dependency caches and
VCS state are large, machine-specific, and meaningless to copy or merge.
"""

from __future__ import annotations

__all__ = ["IGNORED_DIR_NAMES", "ignored_dir_names"]

IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
    }
)


def ignored_dir_names() -> frozenset[str]:
    """Directory names the manifest walk, the merge and the copies skip."""
    return IGNORED_DIR_NAMES
