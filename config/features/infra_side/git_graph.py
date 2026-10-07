"""Git-graph view tuning (the left sidebar's read-only history panel)."""

from typing import TypedDict


class GitGraphConfig(TypedDict):
    """Bounds for ``GET /git/graph`` (read-only; it never writes to a repository)."""

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


GIT_GRAPH: GitGraphConfig = {
    "page_size": 40,
    "max_page_size": 200,
    "timeout_s": 10.0,
    "max_text_chars": 200,
    "max_status_entries": 500,
}
