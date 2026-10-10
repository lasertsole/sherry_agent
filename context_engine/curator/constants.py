"""Filesystem locations and state markers for the curator.

The numeric defaults are NOT restated here: they come from
``config.features.CURATOR_DEFAULTS`` (the TypedDict the docs and the contract
test read), so the two surfaces cannot drift.
"""

from config import ROOT_DIR, SKILLS_DIR, AUTO_SKILLS_DIR
from config.features import CURATOR_DEFAULTS

CURATOR_STATE_FILE = SKILLS_DIR / ".curator_state"
CURATOR_LOGS_DIR = ROOT_DIR / "logs" / "curator"
USAGE_DIR = AUTO_SKILLS_DIR / ".usage"
PINNED_FILE = ".pinned"
# Marker file written inside an archived skill dir naming its absorbing umbrella.
ABSORBED_INTO_FILE = "ABSORBED_INTO"
# Recoverable archive root — the 90-day transition moves skills here instead of
# deleting them. Shared with the agent-side archive/restore path
# (``agent.tools.pub_base.skill_usage._archive_dir``), which reads the same path.
ARCHIVE_DIR = SKILLS_DIR / ".archive"

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

# Bound to the feature registry (single source of truth); names preserved.
DEFAULT_INTERVAL_HOURS = CURATOR_DEFAULTS["default_interval_hours"]
DEFAULT_MIN_IDLE_HOURS = CURATOR_DEFAULTS["default_min_idle_hours"]
DEFAULT_STALE_AFTER_DAYS = CURATOR_DEFAULTS["default_stale_after_days"]
DEFAULT_ARCHIVE_AFTER_DAYS = CURATOR_DEFAULTS["default_archive_after_days"]
DEFAULT_CONSOLIDATE = CURATOR_DEFAULTS["default_consolidate"]

# UI-configurable auto-maintenance interval override (in days). The client allows
# only 1..5 days; anything outside this range is rejected and the file-based
# `interval_hours` (default 5 days) is used.
DEFAULT_INTERVAL_OVERRIDE_MIN_DAYS = CURATOR_DEFAULTS["interval_override_min_days"]
DEFAULT_INTERVAL_OVERRIDE_MAX_DAYS = CURATOR_DEFAULTS["interval_override_max_days"]
