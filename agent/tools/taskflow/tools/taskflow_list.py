"""taskflow_list: the current session's task-flow board.

Like taskflow_summary, this is session-scoped: only flows owned by the calling
session are listed. The registry is shared across sessions, but every read
filters on the session id at the SQL level, so a flow started in another
channel/chat is never visible here. Read-only.
"""

import time
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..registry import store_sqlite
from ._shared import steps_summary

SessionId = Annotated[str, InjectedState("session_id")]
_DESCRIPTION_WIDTH = 40
_CREATOR_WIDTH = 16
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_HEADERS = ("flow_id", "status", "description", "steps", "creator", "updated_at")


def _numeric_ts(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _last_activity_ts(flow: dict) -> float | None:
    """Newest activity stamp persisted anywhere on the flow, or None.

    The schema has no ``updated_at`` column (migration-free, mirroring
    idle detection); activity stamps live inside ``state_json`` (step.dispatched_at,
    result.injected_at) and ``wait_json`` (wait.set_at), so their max is the
    closest honest proxy for "last updated".
    """
    stamps: list[float] = []
    wait = flow.get("wait") or {}
    wait_ts = _numeric_ts(wait.get("set_at"))
    if wait_ts is not None:
        stamps.append(wait_ts)
    state = flow.get("state") or {}
    for step in state.get("steps") or []:
        ts = _numeric_ts(step.get("dispatched_at"))
        if ts is not None:
            stamps.append(ts)
    for result in state.get("results") or []:
        ts = _numeric_ts(result.get("injected_at"))
        if ts is not None:
            stamps.append(ts)
    return max(stamps) if stamps else None


def _format_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime(_TS_FORMAT, time.gmtime(ts))


def _board_row(flow: dict) -> list[str]:
    state = flow.get("state") or {}
    steps = state.get("steps") or []
    counts = steps_summary(steps)
    creator = str(state.get("creator_session_key") or "")
    return [
        str(flow.get("flow_id") or ""),
        str(flow.get("status") or ""),
        str(state.get("description") or "")[:_DESCRIPTION_WIDTH],
        f"{counts.get('done', 0)}/{len(steps)}",
        creator[:_CREATOR_WIDTH],
        _format_ts(_last_activity_ts(flow)),
    ]


def _render_table(rows: list[list[str]]) -> str:
    widths = [len(header) for header in _HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(_HEADERS))]
    lines.append("-+-".join("-" * width for width in widths))
    lines.extend(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)


@tool("taskflow_list")
async def taskflow_list(status_filter: str = "active", session_id: SessionId = "") -> str:
    """List the current session's task flows.

    Only flows owned by this session are shown; other sessions' flows are never
    listed. Read-only, so it needs no expected_revision.

    Args:
        status_filter: "active" (running + waiting, default), "all" (terminal
            flows included), or a specific status name ("running", "waiting",
            "done", "failed", "cancelled").
    """
    normalized = (status_filter or "active").strip().lower()
    flows = store_sqlite.get_all_flows_sync(session_id, normalized)
    if not flows:
        return "No task flows found"
    rows = [_board_row(flow) for flow in flows]
    return f"TaskFlow board ({normalized}): {len(rows)} flow(s)\n{_render_table(rows)}"
