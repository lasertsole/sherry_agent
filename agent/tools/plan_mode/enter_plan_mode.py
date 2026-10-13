"""``enter_plan_mode`` — switch the session into Plan-Then-Execute plan mode.

In plan mode the tool face is narrowed to the read-only whitelist
(``PLAN_MODE["allowed_tools"]``, applied by ``ToolSelectionMiddleware``), the
system prompt carries the four-phase workflow, and a turn may only end with a
``question`` or an ``exit_plan_mode`` call. ``exit_plan_mode`` submits the plan
for the user's approval; on approval the mode exits and the write tools return.

``replan=True`` enters the mode for an ALREADY-APPROVED plan that execution
proved wrong: the status becomes ``needs_replan`` and the next
``exit_plan_mode`` re-binds to a fresh revision.
"""

from typing import Any, override

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from agent.tools.plan_mode.state import PLAN_MODE, set_plan_state
from agent.tools.pub_base import SessionId
from config.features import (
    PLAN_STATUS_PLANNING,
    PLAN_STATUS_REPLAN,
)


class EnterPlanModeInput(BaseModel):
    """Schema for the enter_plan_mode tool arguments."""

    reason: str = Field(
        description="Why this task needs a plan: complexity (multi-file change), "
        "an architectural decision, unclear requirements, or several valid approaches.",
    )
    replan: bool = Field(
        default=False,
        description="True when an ALREADY-APPROVED plan turned out wrong during "
        "execution and needs re-planning (status becomes needs_replan).",
    )
    session_id: SessionId = ""


class EnterPlanModeTool(BaseTool):
    name: str = "enter_plan_mode"
    description: str = (
        "Enter plan mode before executing a complex task: only read-only tools "
        "remain available while you explore and design, then submit the plan "
        "with exit_plan_mode for the user's approval.\n"
        "Use it for: multi-file changes, architectural decisions, unclear or "
        "ambiguous requirements, tasks with several valid approaches.\n"
        "Do NOT use it for a single quick edit or an already-clear question.\n"
        "Pass replan=true when an approved plan failed during execution and "
        "needs to be re-planned."
    )
    args_schema: type[BaseModel] = EnterPlanModeInput
    # idempotent: entering the mode has no side effects beyond a status write.
    metadata: dict = {"idempotent": True, "nudge": True}

    def _enter(self, reason: str, replan: bool, session_id: str) -> str:
        if not PLAN_MODE["enabled"]:
            return "Plan mode is disabled on this deployment; proceed directly."
        target = PLAN_STATUS_REPLAN if replan else PLAN_STATUS_PLANNING
        set_plan_state(session_id, status=target)
        logger.info("enter_plan_mode: session {} -> {}", session_id or "-", target)
        if replan:
            header = (
                "Plan mode is ON (re-planning an approved plan). The previous plan "
                "no longer fits execution; revise it with the new information."
            )
        else:
            header = "Plan mode is ON."
        return (
            f"{header} Reason given: {reason.strip() or '(unspecified)'}\n\n"
            "While plan mode is active ONLY read-only tools are available "
            "(read_file, search_files, message_search, skill_list, skill_view, "
            "question) plus exit_plan_mode itself. Write tools (write_file, "
            "patch_file, terminal, python_repl) are refused until the user "
            "approves the plan.\n\n"
            "Work in four phases:\n"
            "1. Explore — read the relevant files and search the codebase; ask "
            "the user with the `question` tool when a requirement is ambiguous.\n"
            "2. Design — weigh the options, pick one, note the risks.\n"
            "3. Write the plan — goal, scope (must-have / must-not), numbered "
            "steps with acceptance criteria, verification strategy.\n"
            "4. Submit — call exit_plan_mode with the plan for approval.\n\n"
            "A turn in plan mode may only end with a `question` or an "
            "`exit_plan_mode` call."
        )

    @override
    def _run(
        self,
        reason: str,
        replan: bool = False,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        return self._enter(reason, replan, session_id)

    @override
    async def _arun(
        self,
        reason: str,
        replan: bool = False,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        return self._enter(reason, replan, session_id)


def build_enter_plan_mode_tool() -> EnterPlanModeTool:
    tool: EnterPlanModeTool = EnterPlanModeTool()
    tool.handle_tool_error = True
    return tool
