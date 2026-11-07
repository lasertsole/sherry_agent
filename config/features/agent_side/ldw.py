"""LangChain Dynamic Workflow replay settings (vendored core)."""

from typing import TypedDict


class LdwConfig(TypedDict):
    """Replay admission and artifact limits for the LDW integration."""

    #: Replay the plan this many times through TaskFlow before compiling an LDW
    #: workflow. Low-frequency plans never pay the LDW setup cost.
    min_replays: int
    #: Hard cap for a generated ldw__<plan_key>.py script.
    script_max_bytes: int
    #: SQLite journal store used for cross-process resume.
    journal_path: str


LDW: LdwConfig = {
    "min_replays": 3,
    "script_max_bytes": 65_536,
    "journal_path": "src/data/workflow_runs.db",
}
