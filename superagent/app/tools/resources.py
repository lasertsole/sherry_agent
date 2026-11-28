"""Named resources for MCP tools (prototype ``src/app/resources.py``)."""

from __future__ import annotations

from superagent.app.tools.context import builtin_context
from superagent.app.tools.skill_files import read_skill_file

RESOURCES = ("context/admission", "context/delivery", "skills/worker")


def read_resource(name: str) -> str:
    """Read one packaged resource by name."""
    if name not in RESOURCES:
        raise KeyError(name)
    if name.startswith("skills/"):
        return read_skill_file(name.removeprefix("skills/"))
    return builtin_context(name.removeprefix("context/"))
