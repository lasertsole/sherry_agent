"""Vendored langchain-dynamic-workflow core (deepagents-free).

Only the DAG/journal/engine/context/roster surface used by Sherry is exported.
The original package's leaf/middleware/tool modules and its import-time
deepagents dependency are deliberately not part of this entry point.
"""

from ._bootstrap import install_deepagents_stub

install_deepagents_stub()

from ._context import Ctx
from ._dag import DagNode
from ._engine import run_workflow
from ._journal import InMemoryJournalStore, JournalRecord, JournalStore, journal_key
from ._roster import Roster, RosterEntry
from ._schema import to_pydantic_model

try:
    from ._persistence import SqliteWorkflowStore
except ImportError:  # optional sqlite extra
    SqliteWorkflowStore = None  # type: ignore[assignment]

__all__ = [
    "Ctx",
    "DagNode",
    "InMemoryJournalStore",
    "JournalRecord",
    "JournalStore",
    "journal_key",
    "Roster",
    "RosterEntry",
    "SqliteWorkflowStore",
    "to_pydantic_model",
    "run_workflow",
]
