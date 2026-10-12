"""Plan-Then-Execute plan mode tools (``enter_plan_mode`` / ``exit_plan_mode``)."""

from langchain_core.tools import BaseTool

from agent.tools.plan_mode.enter_plan_mode import (
    EnterPlanModeTool,
    build_enter_plan_mode_tool,
)
from agent.tools.plan_mode.exit_plan_mode import (
    ExitPlanModeTool,
    build_exit_plan_mode_tool,
)
from agent.tools.plan_mode.state import PLAN_ACTIVE_STATUSES, PLAN_MODE, set_plan_state

__all__ = [
    "PLAN_ACTIVE_STATUSES",
    "PLAN_MODE",
    "EnterPlanModeTool",
    "ExitPlanModeTool",
    "build_plan_mode_tools",
    "set_plan_state",
]


def build_plan_mode_tools() -> list[BaseTool]:
    """The plan-mode tool pair (empty when the feature is disabled)."""
    if not PLAN_MODE["enabled"]:
        return []
    return [build_enter_plan_mode_tool(), build_exit_plan_mode_tool()]
