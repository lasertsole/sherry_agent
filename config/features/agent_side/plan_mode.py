"""Plan-Then-Execute plan mode: the tool whitelist and the mutation budget."""

from typing import TypedDict


class PlanModeConfig(TypedDict):
    """Plan-Then-Execute plan mode: the tool whitelist and the mutation budget."""

    #: Master switch. Off = the two plan-mode tools are never registered.
    enabled: bool
    #: Longest plan ``exit_plan_mode`` accepts (characters); longer is refused
    #: with a message instead of a truncated plan.
    max_plan_chars: int
    #: How many reject-with-feedback rounds a plan may take. Past it,
    #: ``exit_plan_mode`` stops re-submitting and asks the user to decide, so a
    #: plan cannot be revised forever.
    max_mutations: int
    #: Tools visible (and executable) while plan mode is active: read-only
    #: exploration, the ask-the-user tool, and the two plan tools themselves.
    #: ``sessions_spawn`` is deliberately absent — a child agent runs with its
    #: OWN session's state, so a spawned executor could write files before the
    #: plan is approved and the read-only guarantee would be a formality. Add it
    #: here if that trade is ever wanted.
    allowed_tools: tuple[str, ...]


PLAN_MODE: PlanModeConfig = {
    "enabled": True,
    "max_plan_chars": 20000,
    "max_mutations": 5,
    "allowed_tools": (
        "read_file",
        "search_files",
        "message_search",
        "skill_list",
        "skill_view",
        "question",
        "enter_plan_mode",
        "exit_plan_mode",
    ),
}

#: Status values for ``StateKey.PLAN_STATUS``.
PLAN_STATUS_IDLE = "needs_planning"
PLAN_STATUS_PLANNING = "planning"
PLAN_STATUS_READY = "ready"
PLAN_STATUS_REPLAN = "needs_replan"

#: The statuses in which the tool face is narrowed to ``allowed_tools``.
PLAN_ACTIVE_STATUSES: tuple[str, ...] = (PLAN_STATUS_PLANNING, PLAN_STATUS_REPLAN)
