"""Parallel tool-call safety: batch scheduling and the two process-level locks."""

from typing import TypedDict


class ToolParallelConfig(TypedDict):
    """Switches for the batch-aware tool node and the resource-lock layer."""

    #: Install SafeToolNode in place of LangGraph's ToolNode.
    enabled: bool
    #: False = classify and log only; True = apply the batch plan.
    enforce: bool
    #: Emit one debug/warning line per unsafe batch.
    log_unsafe_batches: bool
    #: Process-wide exclusion for shell / code execution tools.
    system_lock: bool
    #: Per-session exclusion for question / exit_plan_mode.
    interaction_lock: bool


TOOL_PARALLEL: ToolParallelConfig = {
    "enabled": True,
    "enforce": True,
    "log_unsafe_batches": True,
    "system_lock": False,
    "interaction_lock": True,
}
