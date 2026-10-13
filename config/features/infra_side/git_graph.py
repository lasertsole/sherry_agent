"""Git-view tuning (the left sidebar's history panel and its commit reads)."""

from typing import TypedDict


class GitGraphConfig(TypedDict):
    """Bounds for the git routes — the read-only graph and commit reads plus the
    two context-menu write actions (``reset`` / ``checkout`` only)."""

    #: Commits served per page when the caller does not ask for a size.
    page_size: int
    #: Ceiling for a caller-provided ``limit`` (a page is a UI viewport).
    max_page_size: int
    #: Seconds before a git call is killed — a wedged or huge repository must not
    #: hold the request thread.
    timeout_s: float
    #: Longest subject / author / ref text served (a single UI row).
    max_text_chars: int
    #: Cap on the ``git status --porcelain`` line count (only the dirty COUNT is
    #: reported, and a repository with thousands of changes must not be walked).
    max_status_entries: int
    #: Diff rows served for one file (a UI column; the rest is clipped and the
    #: answer carries ``truncated``).
    max_diff_rows: int
    #: File bytes read for one diff side — a bigger blob is flagged instead of
    #: being truncated into a misleading diff.
    max_diff_bytes: int
    #: Local branches served to the panel's branch switcher (most recent commit
    #: first); a repository with more is clipped rather than walked whole.
    max_branches: int


GIT_GRAPH: GitGraphConfig = {
    "page_size": 40,
    "max_page_size": 200,
    "timeout_s": 10.0,
    "max_text_chars": 200,
    "max_status_entries": 500,
    "max_diff_rows": 4000,
    "max_diff_bytes": 2 * 1024 * 1024,
    "max_branches": 200,
}
