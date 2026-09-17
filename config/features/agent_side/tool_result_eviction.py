"""Tool-result message eviction (P0-2) and read_file slicing (P2-4).

Oversized tool results are offloaded to the session's ``evicted/`` directory
and replaced by a recoverable head/tail preview before they ever reach graph
state. Tools whose result already lives on disk (``read_file`` and friends)
are excluded from offloading; ``read_file`` instead gets an execution-time
head slice (see ``pub/func/message/eviction.py``).
"""

from typing import TypedDict


class ToolResultEvictionConfig(TypedDict):
    """Tool-result message eviction configuration."""

    enabled: bool
    # Results over this many characters are evicted to the filesystem
    # (~5000 tokens at the 4 chars/token heuristic).
    evict_threshold_chars: int
    # Preview head/tail line counts.
    preview_head_lines: int
    preview_tail_lines: int
    # Eviction file subdirectory (relative to SESSIONS_DIR/{session_id}/).
    eviction_subdir: str
    # Tools that are never evicted (their result already lives on the backend
    # filesystem / is cheap to recover); ``read_file`` is additionally routed
    # through the execution-time slice path by the middleware.
    excluded_tools: frozenset[str]


TOOL_RESULT_EVICTION: ToolResultEvictionConfig = {
    "enabled": True,
    "evict_threshold_chars": 20_000,
    "preview_head_lines": 5,
    "preview_tail_lines": 5,
    "eviction_subdir": "evicted",
    "excluded_tools": frozenset(
        {
            "read_file",  # file is already on disk, recover with offset/limit
            "write_file",
            "patch_file",
            "search_files",
            "list_files",
            "memory",
            "skill_view",
            "skill_list",
        }
    ),
}
