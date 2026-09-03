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

from context_engine.curator.constants import (
    CURATOR_STATE_FILE as CURATOR_STATE_FILE,
    CURATOR_LOGS_DIR as CURATOR_LOGS_DIR,
    USAGE_DIR as USAGE_DIR,
    PINNED_FILE as PINNED_FILE,
    STATE_ACTIVE as STATE_ACTIVE,
    STATE_STALE as STATE_STALE,
    DEFAULT_INTERVAL_HOURS as DEFAULT_INTERVAL_HOURS,
    DEFAULT_MIN_IDLE_HOURS as DEFAULT_MIN_IDLE_HOURS,
    DEFAULT_STALE_AFTER_DAYS as DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_ARCHIVE_AFTER_DAYS as DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_CONSOLIDATE as DEFAULT_CONSOLIDATE,
)
from context_engine.curator.helpers import (
    _parse_iso as _parse_iso,
    _ensure_dir as _ensure_dir,
    _atomic_json_write as _atomic_json_write,
    _needle_in_path_component as _needle_in_path_component,
)
from context_engine.curator.config import (
    _load_config as _load_config,
    is_enabled as is_enabled,
    get_interval_hours as get_interval_hours,
    get_effective_interval_hours as get_effective_interval_hours,
    get_interval_override_days as get_interval_override_days,
    set_interval_override_days as set_interval_override_days,
    get_last_maintenance_at as get_last_maintenance_at,
    set_last_maintenance_at as set_last_maintenance_at,
    get_min_idle_hours as get_min_idle_hours,
    get_stale_after_days as get_stale_after_days,
    get_archive_after_days as get_archive_after_days,
    get_consolidate as get_consolidate,
)
from context_engine.curator.usage import (
    _skill_record_path as _skill_record_path,
    _skill_dir as _skill_dir,
    _default_record as _default_record,
    load_record as load_record,
    save_record as save_record,
    seed_record_if_missing as seed_record_if_missing,
    set_state as set_state,
    is_pinned as is_pinned,
    _pinned_guard as _pinned_guard,
    _remove_skill as _remove_skill,
    delete_skill as delete_skill,
    agent_created_report as agent_created_report,
)
from context_engine.curator.state import (
    _default_state as _default_state,
    load_state as load_state,
    save_state as save_state,
    is_paused as is_paused,
)
from context_engine.curator.transitions import (
    should_run_now as should_run_now,
    apply_automatic_transitions as apply_automatic_transitions,
)
from context_engine.curator.classify import (
    _classify_removed_skills as _classify_removed_skills,
    _parse_structured_summary as _parse_structured_summary,
    _extract_absorbed_into_declarations as _extract_absorbed_into_declarations,
    _reconcile_classification as _reconcile_classification,
)
from context_engine.curator.report import (
    _build_rename_summary as _build_rename_summary,
    _write_run_report as _write_run_report,
    _render_report_markdown as _render_report_markdown,
)
from context_engine.curator.orchestrator import (
    CURATOR_REVIEW_PROMPT as CURATOR_REVIEW_PROMPT,
    CURATOR_DRY_RUN_BANNER as CURATOR_DRY_RUN_BANNER,
    _render_candidate_list as _render_candidate_list,
    _run_llm_review as _run_llm_review,
    run_curator_review as run_curator_review,
    maybe_run_curator,
)

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
        except Exception:
            pass
        loop.run_until_complete(_a.sleep(_curator_check_interval))


def reset_idle_for_seconds() -> None:
    global _idle_for_seconds
    _idle_for_seconds = 0


_curator_thread: _t.Thread | None = None


def init() -> None:
    """Start the curator background loop; called by the service entry point.

    Used to run at module import time, which made any bare
    ``import context_engine.curator`` (tests, tooling, API consumers) spawn a
    daemon thread unexpectedly (AUDIT_REPORT item 26). Importing this
    package is now side-effect-free.

    Idempotent: subsequent calls are no-ops while the thread is alive.
    """
    global _curator_thread
    if _curator_thread is not None and _curator_thread.is_alive():
        return
    _curator_thread = _t.Thread(target=_curator_loop, daemon=True, name="curator-timer")
    _curator_thread.start()
