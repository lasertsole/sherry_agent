"""Safe text rendering for panel rows (prototype ``src/app/views/text.ts``)."""

from __future__ import annotations

MARKDOWN = ("`", "*", "_", "#", ">", "[", "]")


def plain(value: object) -> str:
    """Render a value as a single non-multiline string."""
    return str(value if value is not None else "").replace("\r", " ").replace("\n", " ")


def preview(value: object, limit: int = 160) -> str:
    """Return a one-line preview."""
    text = plain(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def markdown(value: object) -> str:
    """Strip markdown characters without rendering HTML."""
    text = plain(value)
    for char in MARKDOWN:
        text = text.replace(char, "")
    return text.strip()


def readable(value: object) -> str:
    """Return plain text safe to place in an HTML template."""
    return markdown(value).replace("<", "").replace(">", "")
