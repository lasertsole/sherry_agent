"""Tool inheritance policy for sub-agents — controls which tools are available or blocked.

Two complementary visibility mechanisms:

1. **Name-based deny/allow lists** — the classic mechanism. ``tool_deny`` is
   authoritative when explicitly provided (``None`` triggers the slim
   ``DEFAULT_SUBAGENT_BLOCKED_TOOLS`` fallback); an explicitly empty list means
   "no deny policy" (this is what lets ORCHESTRATOR unblock spawn/yield).
2. **Metadata-based scope tags** — ``tool.metadata["scope"] == "main_only"``
   marks a tool as usable ONLY by the main agent. Such tools are dropped
   unconditionally, BEFORE any allow/deny logic, and cannot be re-granted via
   allow-lists, explicit deny omissions, or ORCHESTRATOR unblock.
"""

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
    """Apply tool policy to a candidate tool list for a sub-agent.

    - ``tool.metadata["scope"] == "main_only"`` tools are dropped FIRST,
      unconditionally — non-overridable by anything below (allow-list, deny
      omissions, ORCHESTRATOR unblock, scope-gap denials).
    - ``tool_deny`` is the **authoritative** deny-list when explicitly provided
      (including an explicitly empty list). The caller controls which tools are
      blocked, and may intentionally omit a normally high-risk tool (e.g. the
      ORCHESTRATOR role unblocking ``sessions_spawn`` / ``sessions_yield`` to
      enable recursive orchestration).
    - ``tool_allow`` (when non-empty) further limits scope to only the listed
      tools (still subject to the main_only metadata gate above).
    - ``DEFAULT_SUBAGENT_BLOCKED_TOOLS`` is a **fallback only** when the caller
      provides no deny policy at all (``tool_deny is None``) — a safety net
      against recursive spawning / privilege escalation. An explicitly empty
      ``tool_deny`` is authoritative and must NOT trigger the fallback
      (otherwise an ORCHESTRATOR whose deny list became empty after unblocking
      spawn/yield would have them re-added).
    - ``blocked_tools`` is merged into the deny-set when provided.

    Every tool that survives the filter (i.e. every tool handed to a subagent)
    is stamped ``metadata["caller_scope"] = "subagent"`` in place, so consumer
    tools can adapt runtime behavior to their caller. Tools retained for the
    main agent are never stamped.

    Args:
        all_tools: Candidate tools (main-agent tool set).
        tool_allow: Explicit allow-list; empty/None means no allow restriction.
        tool_deny: Explicit deny-list; ``None`` means "no policy provided" and
            triggers the default-blocked fallback.
        blocked_tools: Extra blocked tool names merged into the deny-set.

    Returns:
        The filtered, caller-stamped tool list.
    """
    deny_set = set(normalize_tool_denylist(tool_deny))
    allow_set = set(normalize_tool_allowlist(tool_allow))

    # Merge additional blocked tools from caller
    if blocked_tools:
        deny_set.update(blocked_tools)

    # Fallback high-risk block — ONLY when the caller did not provide any
    # deny policy (None). An explicitly empty list is authoritative; the caller
    # may have intentionally emptied it (ORCHESTRATOR unblocks spawn/yield to
    # recurse), and the fallback must not silently re-add them.
    if tool_deny is None:
        deny_set.update(DEFAULT_SUBAGENT_BLOCKED_TOOLS)

    result = []
    for tool in all_tools:
        name = getattr(tool, "name", str(tool))
        metadata = getattr(tool, "metadata", None)
        # Non-overridable scope gate: main_only tools are usable ONLY by the
        # main agent and can never be handed to a subagent — checked BEFORE
        # any allow/deny logic so nothing below can re-grant them.
        if isinstance(metadata, dict) and metadata.get("scope") == "main_only":
            continue
        # Skip tools on the deny-list
        if name in deny_set:
            continue
        # When allow-list is non-empty, only keep explicitly allowed tools
        if allow_set and name not in allow_set:
            continue
        # Stamp the caller scope so consumer tools can adapt at runtime
        if isinstance(metadata, dict):
            metadata.setdefault("caller_scope", "subagent")
        else:
            # Tool lacks a metadata dict (or it is not a dict) — create one.
            # Best-effort: never let stamping failures break the filter.
            try:
                tool.metadata = {"caller_scope": "subagent"}
            except Exception as e:
                logger.debug("Could not stamp caller_scope on tool {}: {}", name, e)
        result.append(tool)

    return result


# Default blocked tools for sub-agents (prevents recursive spawning).
# Kept name-based because ORCHESTRATOR conditionally unblocks spawn/yield and
# metadata cannot express conditionality. The other high-risk tools (memory,
# skill_manage, sessions_kill, sessions_steer) are restricted via the
# non-overridable ``metadata["scope"] = "main_only"`` tag instead.
DEFAULT_SUBAGENT_BLOCKED_TOOLS = [
    "sessions_spawn",
    "sessions_yield",
]
