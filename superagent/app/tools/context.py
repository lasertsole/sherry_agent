"""Built-in context text reader (prototype ``src/app/context.py``)."""

from __future__ import annotations

from pathlib import Path

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"


def builtin_context(name: str) -> str:
    """Read a packaged context file; return an empty string when absent."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid context name")
    path = PROMPTS_ROOT / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""
