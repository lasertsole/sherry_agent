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
    (Unless CURATOR_PROCESS_USELESS_SKILL=delete in .env)
  - Pinned skills bypass all auto-transitions
  - Cron-referenced skills are never auto-transitioned
"""

from skills.curator.constants import (
    ARCHIVE_DIR,
    CURATOR_STATE_FILE,
    CURATOR_LOGS_DIR,
    USAGE_DIR,
    PINNED_FILE,
    STATE_ACTIVE,
    STATE_STALE,
    STATE_ARCHIVED,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MIN_IDLE_HOURS,
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_CONSOLIDATE,
    PROCESS_ARCHIVE,
    PROCESS_DELETE,
)
from skills.curator.helpers import (
    _parse_iso,
    _ensure_dir,
    _atomic_json_write,
    _builtin_skill_names,
    _cron_referenced_skills,
    _needle_in_path_component,
)
from skills.curator.config import (
    _load_config,
    is_enabled,
    get_interval_hours,
    get_min_idle_hours,
    get_stale_after_days,
    get_archive_after_days,
    get_consolidate,
    get_prune_builtins,
    get_process_useless_skill,
)
from skills.curator.usage import (
    _skill_record_path,
    _skill_dir,
    _default_record,
    load_record,
    save_record,
    seed_record_if_missing,
    bump_usage,
    set_state,
    is_pinned,
    pin_skill,
    unpin_skill,
    delete_skill,
    _remove_skill,
    archive_skill,
    restore_skill,
    list_archived,
    agent_created_report,
)
from skills.curator.state import (
    _default_state,
    load_state,
    save_state,
    set_paused,
    is_paused,
)
from skills.curator.transitions import (
    should_run_now,
    apply_automatic_transitions,
)
from skills.curator.classify import (
    _classify_removed_skills,
    _parse_structured_summary,
    _extract_absorbed_into_declarations,
    _reconcile_classification,
)
from skills.curator.report import (
    _build_rename_summary,
    _write_run_report,
    _render_report_markdown,
)
from skills.curator.orchestrator import (
    CURATOR_REVIEW_PROMPT,
    CURATOR_DRY_RUN_BANNER,
    _render_candidate_list,
    _run_llm_review,
    run_curator_review,
    maybe_run_curator,
)
