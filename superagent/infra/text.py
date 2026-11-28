"""Text helpers (prototype ``src/infra/text.ts``).

Bounded rendering for assessment cards, acknowledgements and chat replies.
The limits are deliberately kept here so Sources and app adapters agree on
what "truncated" means.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def words(text: str) -> list[str]:
    """Split text into words for chat word limits."""
    return _WORD.findall(text)


def word_count(text: str) -> int:
    """Return the number of words in ``text``."""
    return len(words(text))


def truncate(text: str, limit: int) -> str:
    """Truncate text to ``limit`` characters without raising."""
    if limit <= 0:
        return ""
    return text[:limit]


def chat_limit(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` at a word boundary; report whether it was truncated."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    cut = text.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = limit
    return text[:cut], True


def fill(template: str, values: dict[str, object]) -> str:
    """Replace ``{name}`` placeholders; unknown names stay literal."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template)


__all__ = ["chat_limit", "fill", "truncate", "word_count", "words"]
