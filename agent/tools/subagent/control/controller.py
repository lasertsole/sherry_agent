"""Controller resolution, permission validation, and control-scope enforcement."""

from ..types.registry import SubagentRunRecord, ExecutionStatus
from ..types.capability import SubagentSessionRole, ControlScope
from ..registry.read import list_runs_for_controller_readonly, get_run_by_child_session_key_readonly
from ..registry.generation import get_latest_run_by_child_session_key


def resolve_controller(session_key: str) -> str | None:
    """Resolve the controller session key for a given sub-agent session."""
    run = get_run_by_child_session_key_readonly(session_key)
    if run is None:
        return None
    if run.controller_session_key:
        return run.controller_session_key
    return run.requester_session_key


def list_controlled_runs(controller_session_key: str) -> list[SubagentRunRecord]:
    """List runs that the given controller session is allowed to control."""
    runs = list_runs_for_controller_readonly(controller_session_key)
    return [
        r for r in runs
        if r.control_scope == ControlScope.CHILDREN or r.requester_session_key == controller_session_key
    ]


def is_run_controllable_by(run: SubagentRunRecord, session_key: str) -> bool:
    """Check whether a session key has control rights over a run."""
    controller = run.controller_session_key or run.requester_session_key
    return session_key == controller


def can_control_run(run: SubagentRunRecord, session_key: str) -> tuple[bool, str]:
    """Validate control permissions with an explanatory deny reason on failure."""
    if run.control_scope == ControlScope.NONE:
        return False, "Control scope NONE: this subagent cannot be controlled"
    controller = run.controller_session_key or run.requester_session_key
    if session_key != controller:
        return False, f"Not controller: session_key={session_key} != controller={controller}"
    return True, ""


def is_self_steer(run: SubagentRunRecord, caller_session_key: str) -> bool:
    """Check whether the caller is the sub-agent itself (self-steer scenario)."""
    return caller_session_key == run.child_session_key


def build_latest_by_session_index(runs: list[SubagentRunRecord]) -> dict[str, SubagentRunRecord]:
    """Index runs by child_session_key, keeping only the latest generation per key."""
    latest: dict[str, SubagentRunRecord] = {}
    for run in runs:
        key = run.child_session_key
        if key not in latest:
            latest[key] = run
        elif run.generation > latest[key].generation:
            latest[key] = run
    return latest
