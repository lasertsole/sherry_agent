"""Control layer for sub-agents: listing, permission checks, kill, steer, and send."""

from .controller import resolve_controller, list_controlled_runs, is_run_controllable_by
from .list import build_subagent_list
from .kill import kill_subagent_run, kill_subagent_run_with_cascade, kill_subagent_run_admin, kill_all_controlled_subagent_runs, list_killable_children
from .steer import steer_subagent_run
from .send import send_subagent_message

__all__ = [
    "resolve_controller",
    "list_controlled_runs",
    "is_run_controllable_by",
    "build_subagent_list",
    "kill_subagent_run",
    "kill_subagent_run_with_cascade",
    "kill_subagent_run_admin",
    "kill_all_controlled_subagent_runs",
    "list_killable_children",
    "steer_subagent_run",
    "send_subagent_message",
]
