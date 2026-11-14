"""Shared text/rendering helpers for the background-review jobs."""

from collections.abc import Sequence
from typing import Any
from pub.func.message.system_reminder import is_system_reminder
from pub.func.message.workspace_notice import is_workspace_notice


def _render_facts_context() -> tuple[str, str]:
    """Return ``(current facts-target content, formatted char limit)`` for review prompts.

    Reads the live content (not the frozen system-prompt snapshot) so a
    compression-time review hands the model what the file holds right now.
    """
    from agent.tools.memory import memory_store

    content = memory_store.format_live_content("facts")
    return content or "(the facts target is empty)", f"{memory_store.facts_char_limit:,}"


def _render_prompt_facts(template: str) -> str:
    """Substitute the FACTS placeholders (injected content may contain braces)."""
    facts_block, facts_limit = _render_facts_context()
    return template.replace("{facts_limit}", facts_limit).replace("{facts_block}", facts_block)


def _msg_to_text(msg: Any) -> str:
    """Flatten a message (or raw string) into plain text."""
    if isinstance(msg, str):
        return msg
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(msg)


def _msg_type(msg: Any) -> str:
    """Return a short role label for a message.

    A workspace notice or catalogue reminder is labelled ``system``: both are
    HumanMessages by role that the user never wrote, so a discarded-slice
    renderer must not read them as the user's words.
    """
    if is_workspace_notice(msg) or is_system_reminder(msg):
        return "system"
    label = getattr(msg, "type", "")
    return str(label) if label else type(msg).__name__.lower()


def _render_discarded_text(discarded_messages: Sequence[Any]) -> str:
    """Render the soon-to-be-discarded slice for background-review / skill-review prompts."""
    return "\n\n".join(f"[{_msg_type(m)}] {_msg_to_text(m)}" for m in discarded_messages)
