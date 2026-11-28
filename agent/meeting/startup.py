"""Boot-time meeting recovery (plan §3.2, 阶段 3).

On process restart any meeting still ``in_progress`` or ``waiting_user`` cannot
resume its in-memory loop (participants/selector live in the old process), so it
is auto-adjourned to ``cancelled`` with the recorded reason ``process_restart``.
Ended/cancelled meetings are untouched. The scan is fail-open and bounded,
following the ``server.service.boot_recovery`` pattern.
"""

from __future__ import annotations

import time

from loguru import logger

from agent.meeting.persistence import MeetingStore

__all__ = ["ADJOURN_REASON", "auto_adjourn_at_boot"]

ADJOURN_REASON = "process_restart"
_MAX_SCAN = 200


async def auto_adjourn_at_boot(store: MeetingStore | None = None) -> dict[str, int]:
    """Cancel every live meeting at boot; return counts by action.

    The reason is written into ``final_summary`` (the schema has no dedicated
    reason column) with a stable ``[adjourned:process_restart]`` prefix so the
    UI and tests can recognize it. ``ended``/``cancelled`` rows are skipped.
    """
    result = {"scanned": 0, "adjourned": 0, "errors": 0}
    try:
        if store is None:
            from agent.meeting.persistence import get_meeting_store

            store = await get_meeting_store()
        meetings = await store.list_meetings()
        live = [m for m in meetings if m["status"] in {"in_progress", "waiting_user"}][:_MAX_SCAN]
        result["scanned"] = len(live)
        for meeting in live:
            try:
                await store.update_meeting(
                    meeting["id"],
                    status="cancelled",
                    ended_at=int(time.time()),
                    final_summary=(
                        f"[adjourned:{ADJOURN_REASON}] meeting was live at process restart"
                    ),
                )
                result["adjourned"] += 1
            except Exception as exc:  # one bad row must not abort the sweep
                logger.warning("failed to adjourn meeting {}: {}", meeting.get("id"), exc)
                result["errors"] += 1
    except Exception as exc:  # fail-open: never block server startup
        logger.exception("meeting boot recovery failed: {}", exc)
        result["errors"] += 1
    return result
