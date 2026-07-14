from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from skills.curator.constants import STATE_ACTIVE, STATE_STALE
from skills.curator.helpers import _parse_iso, _cron_referenced_skills
from skills.curator.config import (
    get_stale_after_days,
    get_archive_after_days,
    is_enabled,
    get_interval_hours,
    get_min_idle_hours,
)
from skills.curator.state import load_state, save_state, is_paused
from skills.curator.usage import (
    agent_created_report,
    seed_record_if_missing,
    set_state,
    _remove_skill,
)


def should_run_now(now: Optional[datetime] = None) -> bool:
    if not is_enabled():
        return False
    if is_paused():
        return False

    state = load_state()
    last = _parse_iso(state.get("last_run_at"))
    if last is None:
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            state["last_run_at"] = now.isoformat()
            state["last_run_summary"] = (
                "deferred first run — curator seeded, will run after one interval"
            )
            save_state(state)
        except Exception as e:
            from loguru import logger
            logger.debug("Failed to seed curator last_run_at: {}", e)
        return False

    if now is None:
        now = datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    interval = timedelta(hours=get_interval_hours())
    return (now - last) >= interval


def apply_automatic_transitions(now: Optional[datetime] = None) -> Dict[str, int]:
    if now is None:
        now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=get_stale_after_days())
    archive_cutoff = now - timedelta(days=get_archive_after_days())

    cron_referenced = _cron_referenced_skills()

    counts: Dict[str, int] = {"marked_stale": 0, "archived": 0, "reactivated": 0, "checked": 0, "seeded": 0}

    for row in agent_created_report():
        counts["checked"] += 1
        name = row["name"]
        if row.get("pinned"):
            continue
        if name in cron_referenced:
            continue

        if not row.get("_persisted", True):
            seed_record_if_missing(name)
            counts["seeded"] += 1
            continue

        last_activity = _parse_iso(row.get("last_activity_at"))
        anchor = last_activity or _parse_iso(row.get("created_at")) or now
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

        current = row.get("state", STATE_ACTIVE)

        never_used = int(row.get("use_count", 0) or 0) == 0
        if never_used and anchor > stale_cutoff:
            if current == STATE_STALE:
                set_state(name, STATE_ACTIVE)
                counts["reactivated"] += 1
            continue

        if anchor <= archive_cutoff and current != "archived":
            ok, _msg = _remove_skill(name)
            if ok:
                counts["archived"] += 1
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            set_state(name, STATE_STALE)
            counts["marked_stale"] += 1
        elif anchor > stale_cutoff and current == STATE_STALE:
            set_state(name, STATE_ACTIVE)
            counts["reactivated"] += 1

    return counts
