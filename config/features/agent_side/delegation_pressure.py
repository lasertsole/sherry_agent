"""Per-turn delegation-pressure budgets.

The gate accumulates READ/exploration calls within one turn. At ``warn_after``
it injects one directive; at ``block_after`` it blocks the configured
read+write tools for the REST OF THE TURN (only ``after_agent`` resets the
counter). Browser debugging tools are explicitly excluded from both counting
and blocking: they are neither reads nor writes for this pressure model.
"""

from typing import TypedDict


class DelegationPressureConfig(TypedDict):
    """Turn-scoped read pressure and the tools it blocks."""

    enabled: bool
    warn_after: int
    block_after: int
    pressure_tools: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    excluded_tools: tuple[str, ...]


#: A main agent that has read/explored 16 times in one turn is done reading:
#: every further read or write errors and points at sessions_spawn. The
#: default pressure set is exploration; ``blocked_tools`` adds the write tools.
DELEGATION_PRESSURE: DelegationPressureConfig = {
    "enabled": False,
    "warn_after": 8,
    "block_after": 16,
    "pressure_tools": (
        "read_file",
        "search_files",
        "message_search",
        "web_search",
        "web_fetch",
        "skill_view",
        "knowledge",
        "terminal",
        "terminal_bg",
        "python_repl",
    ),
    "blocked_tools": (
        "read_file",
        "search_files",
        "message_search",
        "web_search",
        "web_fetch",
        "skill_view",
        "knowledge",
        "terminal",
        "terminal_bg",
        "python_repl",
        "patch_file",
        "write_file",
    ),
    "excluded_tools": (
        "browser",
        "browser_evaluate",
        "browser_navigate",
        "browser_scroll",
        "browser_screenshot",
        "browser_press",
        "browser_type",
        "browser_click",
        "browser_snapshot",
    ),
}
