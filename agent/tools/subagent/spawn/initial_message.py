"""Build the initial user message for a sub-agent using a structured envelope format."""


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
