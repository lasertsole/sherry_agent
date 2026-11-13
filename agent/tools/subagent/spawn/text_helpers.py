"""Small text helpers shared by the spawn pipeline.

Task-name sanitization, the acceptance note, the initial user-message envelope
and thinking-level precedence are pure functions with no shared state; they live
together here instead of in four one-function modules.
"""

import re


def normalize_subagent_task_name(task_name: str | None) -> str | None:
    """Sanitize a task name: replace illegal chars with underscores, collapse repeats, truncate to 64 chars."""
    if task_name is None:
        return None
    normalized = task_name.strip()
    if not normalized:
        return None
    # Replace non-alphanumeric/underscore/hyphen chars
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", normalized)
    # Collapse consecutive underscores into one
    normalized = re.sub(r"_+", "_", normalized)
    # Truncate and strip leading/trailing underscores
    return normalized.strip("_")[:64] or None


def resolve_spawn_accepted_note(
    agent_session_key: str | None = None,
) -> str:
    """Return the acceptance note text for a spawned sub-agent."""
    if agent_session_key:
        return (
            "Subagent spawned in run mode. DO NOT poll for results — "
            "the result will be delivered to you automatically when complete. "
            "Use sessions_yield() to wait for completion."
        )

    return (
        "Subagent spawned in run mode. DO NOT poll for results — "
        "the result will be delivered to you automatically when complete."
    )


def build_subagent_initial_user_message(
    task: str,
    depth: int = 1,
    max_depth: int = 3,
    context: str | None = None,
) -> str:
    """Assemble the first user message with a structured header, task body, and optional context."""
    parts = []

    header_lines = ["[Subagent Context]"]
    header_lines.append(f"Depth: {depth}/{max_depth}")
    parts.append("\n".join(header_lines))

    task_section = f"[Subagent Task]\n{task}"
    parts.append(task_section)

    if context:
        parts.append(f"[Subagent Additional Context]\n{context}")

    parts.append("Begin. Execute the assigned task to completion.")

    return "\n\n".join(parts)


def resolve_thinking_override(
    requester_thinking: str | None = None,
    target_agent_thinking: str | None = None,
    explicit_override: str | None = None,
) -> str | None:
    """Resolve the effective thinking level using the 3-layer precedence hierarchy."""
    if explicit_override and explicit_override.strip():
        return explicit_override.strip()

    if requester_thinking and requester_thinking.strip():
        return requester_thinking.strip()

    if target_agent_thinking and target_agent_thinking.strip():
        return target_agent_thinking.strip()

    return None


def resolve_initial_session_patch(thinking: str | None) -> dict:
    """Build a session-patch dict that injects the thinkingLevel key when a thinking override is active."""
    if not thinking or not thinking.strip():
        return {}
    return {"thinkingLevel": thinking.strip()}
