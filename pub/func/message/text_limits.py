"""The shared inline-text budget for text that rides a model or UI surface.

Four surfaces independently rediscovered the same 8000-character ceiling: a
message preview in the context panel, a user-typed terminal command, a completion
judge's response snippet and the browser's evaluate result. They are one number
by intent — "how much text is worth keeping inline before it stops being a
preview and starts being a payload" — so the value lives here once.
"""

from __future__ import annotations

__all__ = ["MAX_INLINE_TEXT_CHARS", "clip_inline_text"]

#: Longest inline text kept before truncation.
MAX_INLINE_TEXT_CHARS = 8000


def clip_inline_text(
    text: str, limit: int = MAX_INLINE_TEXT_CHARS, *, marker: str = "\n…[truncated]"
) -> tuple[str, bool]:
    """``(text, False)`` when it fits, else ``(text[:limit] + marker, True)``."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + marker, True
