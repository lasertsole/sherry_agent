"""Build the initial user message for a sub-agent using a structured envelope format."""


def build_subagent_initial_user_message(
    task: str,
    depth: int = 1,
    max_depth: int = 3,
    is_persistent_session: bool = False,
    context: str | None = None,
) -> str:
    """Assemble the first user message with a structured header, task body, and optional context."""
    parts = []

    header_lines = [f"[Subagent Context]"]
    header_lines.append(f"Depth: {depth}/{max_depth}")
    if is_persistent_session:
        header_lines.append("This is a persistent session. It will remain active after task completion.")
    parts.append("\n".join(header_lines))

    task_section = f"[Subagent Task]\n{task}"
    parts.append(task_section)

    if context:
        parts.append(f"[Subagent Additional Context]\n{context}")

    parts.append("Begin. Execute the assigned task to completion.")

    return "\n\n".join(parts)
