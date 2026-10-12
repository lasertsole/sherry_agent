"""Per-session access mode for the chat toolbar's shield control.

Four modes — the ZCode-shaped ladder of "how much this session may do":

  - ``plan`` — plan mode (Plan-Then-Execute): read-only tools only until the
    user approves the plan the agent submits. It is derived from the session's
    plan status, so an agent that entered plan mode with ``enter_plan_mode``
    (or the user pinning 计划模式 from this control) reports it here;
  - ``confirm_all`` — strict: every command and every file change asks, no
    matter how ordinary (``hitl:session_confirm_all``);
  - ``auto_edit`` (default) — edits go through the normal gates: the agent edits
    and runs things, and only the dangerous/uncertain calls raise an approval
    card;
  - ``full_access`` — the session runs with the HITL bypass-all flag set
    (``hitl:session_yolo``), so no approval card is raised for this session.

The first and last are the two ends of one setting: writing either clears the
other, so a session is never simultaneously bypassing and asking. All three live
in the session state register the middleware reads on every tool call, so a
switch applies from the next tool call on.

``full_access`` is also what an approval card's "approve all" (YOLO) answer
sets, which is why the mode is derived from the flags rather than stored
separately: a session that answered a card that way reports (and switches back
to) ``full_access`` — and ``plan`` is derived the same way from the plan status
(``StateKey.PLAN_STATUS``), so the control is a VIEW of state the rest of the
system already owns. Switching to any other mode resets the plan status, so a
user who leaves 计划模式 cancels the planning flow.
"""

from agent.middlewares.humanInTheLoop.approval import (
    clear_session_confirm_all,
    clear_session_yolo,
    set_session_confirm_all,
    set_session_yolo,
)
from agent.middlewares.humanInTheLoop.types import (
    HITLConfig,
    SESSION_CONFIRM_ALL_KEY,
    SESSION_YOLO_KEY,
)
from agent.tools.plan_mode.state import read_plan_status, set_plan_state
from config.features import (
    PLAN_ACTIVE_STATUSES,
    PLAN_STATUS_IDLE,
    PLAN_STATUS_PLANNING,
)
from runtime import state_register_mem

#: The four modes the control offers, in presentation order (the read-only
#: design mode first, then strictest to loosest).
PLAN = "plan"
CONFIRM_ALL = "confirm_all"
AUTO_EDIT = "auto_edit"
FULL_ACCESS = "full_access"
ACCESS_MODES = (PLAN, CONFIRM_ALL, AUTO_EDIT, FULL_ACCESS)


def _validate_session_id(session_id: str) -> str:
    """Reject a missing/blank session id (a path-traversal guard shared with the
    other session endpoints)."""
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required")
    cleaned = str(session_id).strip()
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("invalid session_id")
    return cleaned


def get_access_mode(session_id: str) -> str:
    """Current access mode of the session (``auto_edit`` unless a flag is set).

    Reads the same flags the approval pipeline consults, so a session switched to
    full access by an earlier approval card ("approve all") reports it here too.
    YOLO wins over strict when both read as set.
    """
    sid = _validate_session_id(session_id)
    from agent.middlewares.humanInTheLoop.approval import is_yolo_mode

    # Plan mode first: while a plan is being designed the tool face is read-only
    # whatever the approval flags say, so it IS the session's mode (the same
    # state the agent's own enter_plan_mode sets).
    if read_plan_status(sid) in PLAN_ACTIVE_STATUSES:
        return PLAN
    # The session flags are the only source this endpoint reports on: the env /
    # default-based bypass is process-wide, not a per-session choice.
    if is_yolo_mode(HITLConfig(), sid) and state_register_mem.get_state(
        sid, SESSION_YOLO_KEY, False
    ):
        return FULL_ACCESS
    if state_register_mem.get_state(sid, SESSION_CONFIRM_ALL_KEY, False):
        return CONFIRM_ALL
    return AUTO_EDIT


def set_access_mode(session_id: str, mode: str) -> str:
    """Switch the session's access mode and return the applied value.

    ``full_access`` and ``confirm_all`` are mutually exclusive: each write clears
    the other flag, and ``auto_edit`` clears both.
    """
    sid = _validate_session_id(session_id)
    if mode not in ACCESS_MODES:
        raise ValueError(f"mode must be one of {ACCESS_MODES}")
    if mode == PLAN:
        # Pin the mode the agent would otherwise enter by itself; the approval
        # flags are irrelevant while it is on (read-only face), so both are
        # cleared to keep one mode active at a time.
        set_plan_state(sid, status=PLAN_STATUS_PLANNING)
        clear_session_yolo(sid)
        clear_session_confirm_all(sid)
        return mode
    # Any other mode leaves plan mode: an unapproved plan is dropped and an
    # approved one stops being the session's reference.
    set_plan_state(sid, status=PLAN_STATUS_IDLE)
    if mode == FULL_ACCESS:
        set_session_yolo(sid)
    elif mode == CONFIRM_ALL:
        set_session_confirm_all(sid)
    else:
        clear_session_yolo(sid)
        clear_session_confirm_all(sid)
    return mode
