"""Safe reader for packaged skill files (prototype ``src/app/skill_files.py``)."""

from __future__ import annotations

from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


def read_skill_file(relative: str) -> str:
    """Read a skill resource, rejecting paths outside the packaged root."""
    root = SKILLS_ROOT.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("skill path escapes the packaged root")
    return path.read_text(encoding="utf-8") if path.is_file() else ""
