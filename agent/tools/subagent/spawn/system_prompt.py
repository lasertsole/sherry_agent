"""Build structured system prompts for sub-agents based on their role and spawn context."""

from ..types.capability import SubagentSessionRole
from ..types.functional_role import CODE_INTEL_ROLES, FunctionalRole
from ..types.registry import SubagentRunRecord


def build_subagent_system_prompt(
    role: SubagentSessionRole,
    task: str,
    functional_role: FunctionalRole = FunctionalRole.GENERAL,
    role_description: str = "",
    role_prompt_body: str = "",
    requester_label: str = "parent agent",
    depth: int = 1,
    max_depth: int = 3,
    child_session_key: str = "",
    requester_session_key: str = "",
    can_spawn: bool = False,
) -> str:
    """Generate a structured system prompt for a sub-agent, including role, rules, output format, and session context.

    When no functional role is selected (GENERAL with no description/body),
    the role section is byte-identical to the pre-migration prompt.
    """

    sections = []

    has_functional_role = (
        functional_role != FunctionalRole.GENERAL
        or bool(role_description)
        or bool(role_prompt_body)
    )

    # Section 1: Role
    if role == SubagentSessionRole.LEAF:
        if has_functional_role:
            role_desc = (
                f"You are a LEAF worker subagent with the {functional_role.value.upper()} specialization.\n"
                f"You CANNOT spawn further subagents.\n"
            )
            if role_description:
                role_desc += f"{role_description}\n"
            role_desc += "Execute your assigned task directly and report your results."
        else:
            role_desc = (
                "You are a LEAF worker subagent. You CANNOT spawn further subagents.\n"
                "Execute your assigned task directly and report your results."
            )
    elif role == SubagentSessionRole.ORCHESTRATOR:
        if has_functional_role:
            role_desc = (
                f"You are an ORCHESTRATOR subagent with the {functional_role.value.upper()} specialization.\n"
                f"You MAY spawn further subagents using the `sessions_spawn` tool.\n"
            )
            if role_description:
                role_desc += f"{role_description}\n"
            role_desc += "Keep your children's tasks brief and focused."
        else:
            role_desc = (
                "You are an ORCHESTRATOR subagent. You MAY spawn further subagents using the `sessions_spawn` tool.\n"
                "Keep your children's tasks brief and focused."
            )
    else:
        role_desc = "You are a subagent executing a delegated task."

    sections.append(f"## Your Role\n{role_desc}")

    if role_prompt_body:
        sections.append(f"## Role Instructions\n{role_prompt_body}")

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
    sections.append("## Rules\n" + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules)))

    # Section 3: Output Format
    output_format = (
        "## Output Format\n"
        "When you have completed your task, provide a clear summary:\n"
        "```\n"
        "## Task Result\n"
        "**Status**: [completed | failed | partial]\n"
        "**Summary**: [1-2 sentence summary of what was accomplished]\n"
        "**Details**: [key findings, data, or decisions]\n"
        '**Errors**: [any errors encountered, or "None"]\n'
        '**Next Steps**: [recommendations if applicable, or "None"]\n'
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

    # Section 5.5: Code Intelligence (CODE_INTEL_ROLES only — matches the tool
    # injection in spawn/core.py; other roles receive neither the tools nor this
    # guidance).
    if functional_role in CODE_INTEL_ROLES:
        sections.append(
            "## Code Intelligence Tools\n"
            "You have code retrieval tools for fast repo navigation:\n"
            "- `explore` — fuzzy intent → symbol source + call paths. USE FIRST.\n"
            "- `callers` — who calls this symbol\n"
            "- `callees` — what does this symbol call\n"
            "- `impact` — blast radius of modifying a symbol\n"
            "- `semantic_code_search` — concept/intent → ranked code snippets by embedding "
            "similarity. Use when `explore` finds nothing or the query is a concept "
            "('websocket error handling') rather than a symbol name.\n"
            "- `lsp_goto_definition` / `lsp_find_references` — precise, type-aware "
            "jump/refs via the language server (1-based line/character)\n"
            "- `lsp_workspace_symbol` — fuzzy workspace symbol search\n"
            "- `lsp_call_hierarchy` — callers (incoming) / callees (outgoing) of a symbol\n"
            "- `lsp_rename` — workspace rename; PREVIEWS by default (dry_run=true), "
            "set dry_run=false to apply\n"
            "- `lsp_diagnostics` — errors/warnings for a file (waits for the async "
            "publishDiagnostics notification)\n"
            "- `lsp_format` — formatting edits; PREVIEWS by default (write=false), "
            "reports supported=false rather than faking success\n"
            "- `lsp_status` — honest availability of every configured language server "
            "(available / not_installed); starts nothing\n"
            "Workflow: explore(query) → lsp_find_references / callers for precision → "
            "terminal (rg/grep) as keyword fallback.\n"
            "Index is built on first use; subsequent queries are fast. LSP servers "
            "start on demand and are reaped when idle; if a server is missing you get "
            "an install hint and the explore/terminal fallback."
        )

    # Section 6: Session Context
    context_lines = [
        f"  Your session key: {child_session_key}",
        f"  Parent session: {requester_session_key}",
        f"  Depth: {depth} / {max_depth}",
    ]
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
            f'- [{child.run_id[:8]}] "{label}" | depth={child.depth} | '
            f"role={child.role} | session={child.child_session_key}"
        )

    lines.append(
        "\nUse `sessions_yield` to wait for them to complete (push-based, do NOT poll).\n"
        "Use `subagents_list` to check their status.\n"
        "Use `sessions_kill` to cancel a subagent.\n"
        "Use `sessions_steer` to redirect a subagent.\n"
    )

    return "\n".join(lines)
