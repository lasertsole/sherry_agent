"""Build structured system prompts for sub-agents based on their role and spawn context."""

from dataclasses import dataclass
from typing import Any

from ..types.capability import SubagentSessionRole
from ..types.functional_role import (
    CODE_INTEL_ROLES,
    CODE_INTEL_WRITE_ROLES,
    PTC_ROLES,
    SPAWN_ALLOWED_CHILD_ROLES,
    FunctionalRole,
)
from ..types.registry import SubagentRunRecord

__all__ = [
    "SubagentPromptContext",
    "build_subagent_system_prompt",
    "build_subagent_system_prompt_from_context",
    "build_active_subagents_section",
]


@dataclass(frozen=True)
class SubagentPromptContext:
    """Everything the prompt sections read, in one immutable object (M1).

    The section builders below take this context instead of an 11-parameter
    argument list; adding a section that needs a new input adds one field here
    rather than another positional parameter at every call site.
    """

    role: SubagentSessionRole
    task: str
    functional_role: FunctionalRole = FunctionalRole.GENERAL
    role_description: str = ""
    role_prompt_body: str = ""
    requester_label: str = "parent agent"
    depth: int = 1
    max_depth: int = 3
    child_session_key: str = ""
    requester_session_key: str = ""
    can_spawn: bool = False


def build_subagent_system_prompt_from_context(ctx: SubagentPromptContext) -> str:
    """Generate a structured system prompt for a sub-agent from one context."""
    role = ctx.role
    functional_role = ctx.functional_role
    role_description = ctx.role_description
    role_prompt_body = ctx.role_prompt_body
    depth = ctx.depth
    max_depth = ctx.max_depth
    child_session_key = ctx.child_session_key
    requester_session_key = ctx.requester_session_key
    can_spawn = ctx.can_spawn

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
    ]
    if can_spawn:
        rules.append(
            "Do NOT poll for child completion. Completion notifications are push-based — you will be resumed automatically when children finish. Do NOT call `sessions_yield` or `subagents_list` in a loop to check if children are done."
        )
    rules.append(
        "If your output may be large, summarize the key points. Your full output may be truncated to fit delivery limits."
    )
    if can_spawn:
        rules.append(
            "When spawning child subagents, keep their tasks brief and focused. Track their session keys.\n"
            "Use `sessions_yield` ONCE to signal you are waiting — this is a signal, not a poll.\n"
            "Use `sessions_send` to message a running child; child management tools "
            "(`sessions_kill` / `sessions_steer`) belong to the main agent only."
        )
    sections.append("## Rules\n" + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules)))

    # Section 3: Output Format
    #
    # The handoff block goes FIRST: both the stored-result cap and the delivery
    # cap truncate from the TAIL, so a block after long Details can be cut off.
    output_format = (
        "## Output Format\n"
        "When you have completed your task, output a handoff JSON block FIRST, "
        "then the summary.\n"
        "```json\n"
        "{\n"
        '  "task_understanding": "<your own restatement of the task>",\n'
        '  "discovered_hard_constraints": ["<hard constraint YOU found>"],\n'
        '  "locked_decisions": [{"decision": "<what you locked in>", '
        '"reason": "<why>"}],\n'
        '  "failed_branches": [{"branch": "<what you tried>", '
        '"failure_reason": "<why it failed>"}],\n'
        '  "success_steps": [{"step": "<a DIRECT step to completion>", '
        '"tool": "<tool used, if any>", "params": "<key parameters>", '
        '"outcome": "<what it produced>"}]\n'
        "}\n"
        "```\n"
        "Rules for the handoff: fill only what YOU encountered in THIS layer — "
        "do NOT include child subagents' data. Empty lists stay empty "
        "(``[]``). `failed_branches` lists abandoned approaches/directions; "
        "`success_steps` lists ONLY the minimal ordered steps that led straight "
        "to completion (exclude exploration, debugging and abandoned branches; "
        "empty when the task failed). Do NOT add depth/agent_label/children/"
        "outcome: the system injects those.\n\n"
        "Then provide the summary:\n"
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

    # Section 5: Sub-Agent Spawning (conditional on the NODE, not the role):
    # a non-leaf gets both this guidance and the spawn/management tools; a leaf
    # gets neither (the tool policy strips the whole control set).
    if can_spawn:
        allowed = SPAWN_ALLOWED_CHILD_ROLES.get(functional_role, frozenset({functional_role}))
        if functional_role == FunctionalRole.GENERAL:
            allowed_text = "any functional role (general is unrestricted)"
        else:
            allowed_text = " / ".join(
                role.value for role in sorted(allowed, key=lambda item: item.value)
            )
        read_only_note = ""
        if functional_role in CODE_INTEL_ROLES and functional_role not in CODE_INTEL_WRITE_ROLES:
            read_only_note = (
                "\n- Your children are read-only research workers; you still do not edit files."
            )
        spawn_guidance = (
            "## Sub-Agent Spawning\n"
            "You may spawn child subagents via `sessions_spawn`.\n"
            "Guidelines:\n"
            f"- Allowed child roles: {allowed_text}.{read_only_note}\n"
            "- Keep child tasks small and focused.\n"
            "- After spawning, use `sessions_yield` to signal you are waiting — do NOT poll.\n"
            "- You will be automatically resumed when children complete.\n"
            "- Use `subagents_list` to check status if needed.\n"
            "- Use `sessions_send` to message a running child.\n"
            "- `sessions_kill` / `sessions_steer` are main-agent only; a subagent "
            "does not manage siblings.\n"
            f"- Maximum nesting depth: {max_depth}. You are at depth {depth}.\n"
            f"- You can spawn up to depth {max_depth} (leaf agents cannot spawn further)."
        )
        sections.append(spawn_guidance)

    # Section 5.5: Code Intelligence (CODE_INTEL_ROLES — matches the tool
    # injection in spawn/core.py; other roles receive neither the tools nor this
    # guidance). The tool LIST is shared; the rename/format guidance branches:
    # read-only roles stay in preview mode, write roles may apply.
    if functional_role in CODE_INTEL_ROLES:
        can_write = functional_role in CODE_INTEL_WRITE_ROLES
        intro = (
            "## Code Intelligence Tools\n"
            "You have code retrieval tools for fast repo navigation and impact analysis "
            "BEFORE editing:\n"
            if can_write
            else "## Code Intelligence Tools\n"
            "You have code retrieval tools for fast repo navigation:\n"
        )
        rename_line = (
            "- `lsp_rename` — workspace rename; PREVIEWS by default (dry_run=true). "
            "You MAY pass dry_run=false to apply a cross-file symbol rename when it is "
            "the right edit — it is semantically safer than a manual patch.\n"
            if can_write
            else "- `lsp_rename` — workspace rename; PREVIEWS by default (dry_run=true). "
            "Never pass dry_run=false: your role is read-only.\n"
        )
        format_line = (
            "- `lsp_format` — formatting edits; PREVIEWS by default (write=false). You "
            "MAY pass write=true to apply formatting after an edit.\n"
            if can_write
            else "- `lsp_format` — formatting edits; PREVIEWS by default (write=false), "
            "reports supported=false rather than faking success. Never pass "
            "write=true: your role is read-only.\n"
        )
        workflow = (
            "Workflow: explore(query) / callers / impact / lsp_find_references to "
            "understand the blast radius, then edit. Do LIGHTWEIGHT lookups directly; "
            "spawn a researcher for a heavy multi-step investigation.\n"
            if can_write
            else "Workflow: explore(query) → lsp_find_references / callers for precision → "
            "terminal (rg/grep) as keyword fallback.\n"
        )
        sections.append(
            intro + "- `explore` — fuzzy intent → symbol source + call paths. USE FIRST.\n"
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
            + rename_line
            + "- `lsp_diagnostics` — errors/warnings for a file (waits for the async "
            "publishDiagnostics notification)\n"
            + format_line
            + "- `lsp_status` — honest availability of every configured language server "
            "(available / not_installed); starts nothing\n"
            + workflow
            + "Index is built on first use; subsequent queries are fast. LSP servers "
            "start on demand and are reaped when idle; if a server is missing you get "
            "an install hint and the explore/terminal fallback."
        )

    # Section 5.6: Programmatic Tool Calling (PTC_ROLES only — matches the
    # execute_code injection in spawn/core.py; other roles receive neither the
    # tool nor this guidance).
    if functional_role in PTC_ROLES:
        sections.append(
            "## Programmatic Tool Calling (execute_code)\n"
            "You have an `execute_code` tool that runs a Python script with "
            "synchronous access to your tools via `from sherry_tools import ...`.\n"
            "Use it when:\n"
            "- You need 3+ tool calls with processing logic between them\n"
            "- You must filter/reduce large tool outputs before they enter your context\n"
            "- You need conditional branching or loops over tool calls\n"
            "Do NOT use it for a single tool call, tasks that need complex reasoning "
            "over the full result, or interactive user input.\n"
            "The script runs in an isolated child process with restricted builtins "
            "(no `open`, no arbitrary imports) and a hard tool-call budget. "
            "Print your final result to stdout."
        )

    # Section 6: Session Context
    context_lines = [
        f"  Your session key: {child_session_key}",
        f"  Parent session: {requester_session_key}",
        f"  Depth: {depth} / {max_depth}",
    ]
    sections.append("## Session Context\n" + "\n".join(context_lines))

    return "\n\n".join(sections)


def build_subagent_system_prompt(role: SubagentSessionRole, task: str, **kwargs: Any) -> str:
    """Compatibility entry point: build the context, then the prompt.

    The context object above is the single input of the section builder; this
    wrapper keeps the historical ``(role, task, **fields)`` call shape used by
    tests and older call sites without re-introducing an 11-parameter list.
    """
    return build_subagent_system_prompt_from_context(
        SubagentPromptContext(role=role, task=task, **kwargs)
    )


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
