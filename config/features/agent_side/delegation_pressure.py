"""Per-turn delegation-pressure budgets.

The gate accumulates READ/exploration calls within one turn. At
``plan_nudge_after`` it nudges the agent to plan (todowrite / TaskFlow) when it
has read without creating either; at ``warn_after`` it injects one
system-reminder directive; at ``block_after`` it injects an escalated
system-reminder. Tools remain available — this gate advises, never
blocks. Browser debugging tools are explicitly excluded from counting: they
are neither reads nor writes for this pressure model.
"""

from typing import TypedDict


class DelegationPressureConfig(TypedDict):
    """Turn-scoped read pressure and its escalation thresholds."""

    enabled: bool
    plan_nudge_after: int
    warn_after: int
    block_after: int
    pressure_tools: tuple[str, ...]
    excluded_tools: tuple[str, ...]


#: A main agent that has read/explored 12 times in one turn receives an
#: escalated directive to delegate; tools remain available.
DELEGATION_PRESSURE: DelegationPressureConfig = {
    "enabled": True,
    #: Reads before the one-time "plan first" nudge (todowrite / TaskFlow).
    "plan_nudge_after": 5,
    "warn_after": 7,
    "block_after": 12,
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
