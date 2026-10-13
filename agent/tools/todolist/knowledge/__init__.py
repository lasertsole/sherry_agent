"""Knowledge tool family: ``knowledge`` (write / read / list plan knowledge).

``build_knowledge_tools`` mirrors the other tool families: it tags the tool
with ``handle_tool_error=True`` and merges the shared ``nudge`` +
``scope=main_only`` metadata contract (nudge allowlist + subagent tool policy).

Access is isolated per plan identity — ``identity.resolve_plan_identity`` /
``identity.associated_plan_identities`` derive the storage key from the
canonical plan path so same-named plans in different sessions stay physically
isolated while collaborators of one plan file share a directory;
``ownership.association_plan_refs`` remains the raw association-source layer.
``clear_session_plan_knowledge`` backs the session-teardown purge.
"""

from langchain_core.tools import BaseTool

from .identity import (
    PlanIdentity,
    associated_plan_identities,
    fallback_plan_name,
    plan_key,
    resolve_plan_identity,
    resolve_session_plan_identity,
)
from .knowledge_store import KnowledgeStore, Layer, clear_session_plan_knowledge
from .knowledge_tool import knowledge
from .ownership import associated_plan_names, is_plan_associated

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
    "PlanIdentity",
    "_KNOWLEDGE_TOOLS",
    "associated_plan_identities",
    "associated_plan_names",
    "build_knowledge_tools",
    "clear_session_plan_knowledge",
    "fallback_plan_name",
    "is_plan_associated",
    "knowledge",
    "plan_key",
    "resolve_plan_identity",
    "resolve_session_plan_identity",
]
