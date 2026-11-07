"""Stable import surface for the vendored LDW core.

The vendored package installs its own minimal ``deepagents`` stub at import
time; callers should use this module instead of importing ``vendor.ldw``
directly, so a future swap of the vendoring strategy stays local.
"""

from __future__ import annotations

from vendor.ldw import (  # noqa: F401  (re-exported public seam)
    Ctx,
    DagNode,
    InMemoryJournalStore,
    JournalRecord,
    JournalStore,
    Roster,
    RosterEntry,
    SqliteWorkflowStore,
    run_workflow,
)

__all__ = [
    "Ctx",
    "DagNode",
    "InMemoryJournalStore",
    "JournalRecord",
    "JournalStore",
    "Roster",
    "RosterEntry",
    "SqliteWorkflowStore",
    "run_workflow",
]
