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
    #: Aggregate char threshold for the results of one parallel tool batch.
    #: 0 disables the check. Default 100K (~25K tokens) so a normal 2-5 call
    #: batch never trips it; 100 results under the per-call 20K eviction limit
    #: still sum to ~1.9M and must be held here.
    batch_result_threshold_chars: int


TOOL_PARALLEL: ToolParallelConfig = {
    "enabled": True,
    "enforce": True,
    "log_unsafe_batches": True,
    "system_lock": False,
    "interaction_lock": True,
    "batch_result_threshold_chars": 100_000,
}
