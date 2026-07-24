"""Build structured system prompts for sub-agents based on their role and spawn context."""

from ..types.capability import SubagentSessionRole
from ..types.registry import SubagentRunRecord, ExecutionStatus


def build_subagent_system_prompt(
    role: SubagentSessionRole,
    task: str,
    requester_label: str = "parent agent",
    depth: int = 1,
    max_depth: int = 3,
    child_session_key: str = "",
    requester_session_key: str = "",
    can_spawn: bool = False,
    is_persistent_session: bool = False,
) -> str:
    """Generate a structured system prompt for a sub-agent, including role, rules, output format, and session context."""

    sections = []

    # Section 1: Role
    if role == SubagentSessionRole.LEAF:
        role_desc = (
            "You are a LEAF worker subagent. You CANNOT spawn further subagents.\n"
            "Execute your assigned task directly and report your results."
        )
    elif role == SubagentSessionRole.ORCHESTRATOR:
        role_desc = (
            "You are an ORCHESTRATOR subagent. You MAY spawn further subagents using the `sessions_spawn` tool.\n"
            "Keep your children's tasks brief and focused."
        )
    else:
        role_desc = "You are a subagent executing a delegated task."

    sections.append(f"## Your Role\n{role_desc}")

    # Section 2: Rules
    rules = [
        "Focus ONLY on the assigned task. Do not take proactive actions beyond your task scope.",
        "Report your results concisely when done. Include key findings, decisions, and any data requested.",
        "If you encounter an error you cannot resolve, report it clearly and finish.",
        "Do NOT poll for child completion. Completion notifications are push-based — you will be resumed automatically when children finish. Do NOT call `sessions_yield` or `subagents_list` in a loop to check if children are done.",
        "If your output may be large, summarize the key points. Your full output may be truncated to fit delivery limits.",
    ]
    if can_spawn:
        rules.append(
            "When spawning child subagents, keep their tasks brief and focused. Track their session keys.\n"
            "Use `sessions_yield` ONCE to signal you are waiting — this is a signal, not a poll.\n"
            "Use `sessions_kill` to cancel unneeded subagents.\n"
            "Use `sessions_steer` to redirect subagents with new instructions."
        )
    sections.append("## Rules\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules)))

    # Section 3: Output Format
    output_format = (
        "## Output Format\n"
        "When you have completed your task, provide a clear summary:\n"
        "```\n"
        "## Task Result\n"
        "**Status**: [completed | failed | partial]\n"
        "**Summary**: [1-2 sentence summary of what was accomplished]\n"
        "**Details**: [key findings, data, or decisions]\n"
        "**Errors**: [any errors encountered, or \"None\"]\n"
        "**Next Steps**: [recommendations if applicable, or \"None\"]\n"
        "```\n"
        "Keep your output concise. Long outputs will be truncated."
    )
    sections.append(output_format)

    # Section 4: What You DON'T Do
    dont_do = (
        "## What You DON'T Do\n"
        "- You do NOT make decisions outside your task scope.\n"
        "- You do NOT modify resources unrelated to your task.\n"
        "- You do NOT spawn subagents unless you are an ORCHESTRATOR."
    )
    sections.append(dont_do)

    # Section 5: Sub-Agent Spawning (conditional)
    if can_spawn:
        spawn_guidance = (
            "## Sub-Agent Spawning\n"
            "You may spawn child subagents via `sessions_spawn`.\n"
            "Guidelines:\n"
            "- Keep child tasks small and focused.\n"
            "- After spawning, use `sessions_yield` to signal you are waiting — do NOT poll.\n"
            "- You will be automatically resumed when children complete.\n"
            "- Use `subagents_list` to check status if needed.\n"
            "- Use `sessions_kill` to cancel a child that is no longer needed.\n"
            f"- Maximum nesting depth: {max_depth}. You are at depth {depth}.\n"
            f"- You can spawn up to depth {max_depth} (leaf agents cannot spawn further)."
        )
        sections.append(spawn_guidance)

    # Section 6: Session Context
    context_lines = [
        f"  Your session key: {child_session_key}",
        f"  Parent session: {requester_session_key}",
        f"  Depth: {depth} / {max_depth}",
    ]
    if is_persistent_session:
        context_lines.append("  This is a PERSISTENT session — it will remain active after your task.")
    sections.append("## Session Context\n" + "\n".join(context_lines))

    return "\n\n".join(sections)


def build_active_subagents_section(
    active_children: list[SubagentRunRecord],
) -> str:
    """Build the 'Active Subagents' system-prompt section for a parent agent.

    Designed to be called by the parent agent's middleware or prompt builder at
    the start of each turn — NOT by the spawn/announce pipeline internally.
    """
    if not active_children:
        return ""

    lines = ["\n## Active Subagents\n"]
    lines.append(f"You have {len(active_children)} active subagent(s):\n")

    for child in active_children:
        label = child.label or child.task[:60]
        lines.append(
            f"- [{child.run_id[:8]}] \"{label}\" | depth={child.depth} | "
            f"role={child.role} | session={child.child_session_key}"
        )

    lines.append(
        "\nUse `sessions_yield` to wait for them to complete (push-based, do NOT poll).\n"
        "Use `subagents_list` to check their status.\n"
        "Use `sessions_kill` to cancel a subagent.\n"
        "Use `sessions_steer` to redirect a subagent.\n"
    )

    return "\n".join(lines)
