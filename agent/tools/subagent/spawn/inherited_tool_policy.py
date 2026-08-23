"""Tool inheritance policy for sub-agents — controls which tools are available or blocked."""

from loguru import logger


def normalize_tool_denylist(deny: list[str] | None) -> list[str]:
    """Deduplicate a tool deny-list while preserving insertion order."""
    if not deny:
        return []
    return list(dict.fromkeys(deny))


def normalize_tool_allowlist(allow: list[str] | None) -> list[str]:
    """Deduplicate a tool allow-list while preserving insertion order."""
    if not allow:
        return []
    return list(dict.fromkeys(allow))


def apply_tool_policy(
    all_tools: list,
    tool_allow: list[str] | None,
    tool_deny: list[str] | None,
    blocked_tools: list[str] | None = None,
) -> list:
    """Apply tool policy: deny-list takes priority, allow-list limits scope, and high-risk tools are blocked by default."""
    deny_set = set(normalize_tool_denylist(tool_deny))
    allow_set = set(normalize_tool_allowlist(tool_allow))

    # Merge additional blocked tools from caller
    if blocked_tools:
        deny_set.update(blocked_tools)

    # Always block high-risk tools to prevent sub-agent abuse (recursive spawn, privilege escalation)
    default_blocked = ["sessions_spawn", "sessions_yield", "skill_manage", "memory"]
    deny_set.update(default_blocked)

    result = []
    for tool in all_tools:
        name = getattr(tool, "name", str(tool))
        # Skip tools on the deny-list
        if name in deny_set:
            continue
        # When allow-list is non-empty, only keep explicitly allowed tools
        if allow_set and name not in allow_set:
            continue
        result.append(tool)

    return result


# Default blocked tools for sub-agents (prevents recursive spawning and privilege escalation)
DEFAULT_SUBAGENT_BLOCKED_TOOLS = [
    "sessions_spawn",
    "sessions_yield",
    "sessions_kill",
    "sessions_steer",
    "skill_manage",
    "memory",
]
