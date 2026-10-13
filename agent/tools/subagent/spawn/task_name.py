"""Normalization of sub-agent task names — sanitization and truncation."""

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
