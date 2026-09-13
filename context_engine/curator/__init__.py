"""Curator — background skill maintenance orchestrator.

The curator is a background task that periodically reviews agent-created
skills and maintains the collection. It runs inactivity-triggered: when the
agent is idle and the last curator run was longer than ``interval_hours``
ago, ``maybe_run_curator()`` spawns a background task to do the review.

Responsibilities:
  - Auto-transition lifecycle states based on derived skill activity timestamps
  - Consolidate overlapping skills into class-level umbrellas (opt-in LLM pass)
  - Persist curator state (last_run_at, paused, etc.) in .curator_state

Strict invariants:
  - Only touches agent-created skills (under skills/auto/), never built-ins
  - Never auto-deletes — only archives. Archive is recoverable.
  - Pinned skills bypass all auto-transitions
"""

from loguru import logger

from context_engine.curator.constants import (
    CURATOR_STATE_FILE,
    CURATOR_LOGS_DIR,
    USAGE_DIR,
    PINNED_FILE,
    STATE_ACTIVE,
    STATE_STALE,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MIN_IDLE_HOURS,
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_CONSOLIDATE,
)
from context_engine.curator.config import (
    is_enabled,
    get_interval_hours,
    get_effective_interval_hours,
    get_interval_override_days,
    set_interval_override_days,
    get_last_maintenance_at,
    set_last_maintenance_at,
    get_min_idle_hours,
    get_stale_after_days,
    get_archive_after_days,
    get_consolidate,
)
from context_engine.curator.usage import (
    load_record,
    save_record,
    seed_record_if_missing,
    set_state,
    is_pinned,
    pin_skill,
    unpin_skill,
    delete_skill,
    agent_created_report,
)
from context_engine.curator.state import (
    load_state,
    save_state,
    is_paused,
)
from context_engine.curator.transitions import (
    should_run_now,
    apply_automatic_transitions,
)
from context_engine.curator.orchestrator import (
    CURATOR_REVIEW_PROMPT,
    CURATOR_DRY_RUN_BANNER,
    run_curator_review,
    maybe_run_curator,
)

__all__ = [
    "CURATOR_STATE_FILE",
    "CURATOR_LOGS_DIR",
    "USAGE_DIR",
    "PINNED_FILE",
    "STATE_ACTIVE",
    "STATE_STALE",
    "DEFAULT_INTERVAL_HOURS",
    "DEFAULT_MIN_IDLE_HOURS",
    "DEFAULT_STALE_AFTER_DAYS",
    "DEFAULT_ARCHIVE_AFTER_DAYS",
    "DEFAULT_CONSOLIDATE",
    "is_enabled",
    "get_interval_hours",
    "get_effective_interval_hours",
    "get_interval_override_days",
    "set_interval_override_days",
    "get_last_maintenance_at",
    "set_last_maintenance_at",
    "get_min_idle_hours",
    "get_stale_after_days",
    "get_archive_after_days",
    "get_consolidate",
    "load_record",
    "save_record",
    "seed_record_if_missing",
    "set_state",
    "is_pinned",
    "pin_skill",
    "unpin_skill",
    "delete_skill",
    "agent_created_report",
    "load_state",
    "save_state",
    "is_paused",
    "should_run_now",
    "apply_automatic_transitions",
    "CURATOR_REVIEW_PROMPT",
    "CURATOR_DRY_RUN_BANNER",
    "run_curator_review",
    "maybe_run_curator",
    "init",
    "reset_idle_for_seconds",
]

# run curator to maintain auto-skills
import threading as _t

_curator_check_interval: int = 3600
_idle_for_seconds: int = 0


def _curator_loop():
    import asyncio as _a

    loop = _a.new_event_loop()
    _a.set_event_loop(loop)

    global _idle_for_seconds
    global _curator_check_interval
    while True:
        try:
            _idle_for_seconds += _curator_check_interval
            maybe_run_curator(idle_for_seconds=_idle_for_seconds)
        except Exception as e:
            logger.error("Curator cycle failed: {}", e)
        loop.run_until_complete(_a.sleep(_curator_check_interval))


def reset_idle_for_seconds() -> None:
    global _idle_for_seconds
    _idle_for_seconds = 0


_curator_thread: _t.Thread | None = None


def init() -> None:
    """Start the curator background loop; called by the service entry point.

    Used to run at module import time, which made any bare
    ``import context_engine.curator`` (tests, tooling, API consumers) spawn a
    daemon thread unexpectedly. Importing this package is now
    side-effect-free.

    Idempotent: subsequent calls are no-ops while the thread is alive.
    """
    global _curator_thread
    if _curator_thread is not None and _curator_thread.is_alive():
        return
    _curator_thread = _t.Thread(target=_curator_loop, daemon=True, name="curator-timer")
    _curator_thread.start()
