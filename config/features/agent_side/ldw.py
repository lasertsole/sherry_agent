"""LangChain Dynamic Workflow replay settings (vendored core)."""

from typing import TypedDict


class LdwConfig(TypedDict):
    """Replay admission and artifact limits for the LDW integration."""

    #: Master switch. False keeps every replay on the TaskFlow path.
    enabled: bool
    #: Replay the plan this many times through TaskFlow before compiling an LDW
    #: workflow. Low-frequency plans never pay the LDW setup cost.
    min_replays: int
    #: Hard cap for a generated ldw__<plan_key>.py script.
    script_max_bytes: int
    #: SQLite journal store used for cross-process resume.
    journal_path: str
    #: Automatically create a TaskFlow replay flow when LDW replay fails.
    auto_degrade_enabled: bool
    #: Run the repair extraction/recompile loop after a TaskFlow fallback.
    repair_enabled: bool
    #: Wall-clock ceiling for one LDW run (a hung leaf degrades to TaskFlow).
    real_replay_timeout_s: float
    #: A fallback is refused when more than this many steps failed (runaway guard).
    max_failed_steps: int


LDW: LdwConfig = {
    "enabled": True,
    "min_replays": 3,
    "script_max_bytes": 65_536,
    "journal_path": "src/data/workflow_runs.db",
    "auto_degrade_enabled": True,
    "repair_enabled": True,
    "real_replay_timeout_s": 900.0,
    "max_failed_steps": 5,
}
