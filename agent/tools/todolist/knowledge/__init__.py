"""Knowledge tool family: ``knowledge`` (write / read / list plan knowledge).

``build_knowledge_tools`` mirrors the other tool families: it tags the tool
with ``handle_tool_error=True`` and merges the shared ``nudge`` +
``scope=main_only`` metadata contract (nudge allowlist + subagent tool policy).
"""

from langchain_core.tools import BaseTool

from .knowledge_tool import knowledge
from .knowledge_store import KnowledgeStore, Layer

_KNOWLEDGE_TOOLS: list[BaseTool] = [knowledge]


def build_knowledge_tools() -> list[BaseTool]:
    """Build and return the knowledge tool for main-agent wiring."""
    for t in _KNOWLEDGE_TOOLS:
        t.handle_tool_error = True
        t.metadata = {**(t.metadata or {}), "nudge": True, "scope": "main_only"}
    return list(_KNOWLEDGE_TOOLS)


__all__ = [
    "KnowledgeStore",
    "Layer",
    "_KNOWLEDGE_TOOLS",
    "build_knowledge_tools",
    "knowledge",
]
