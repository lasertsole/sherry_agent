from config import ROOT_DIR, SKILLS_DIR, AUTO_SKILLS_DIR

CURATOR_STATE_FILE = SKILLS_DIR / ".curator_state"
CURATOR_LOGS_DIR = ROOT_DIR / "logs" / "curator"
USAGE_DIR = AUTO_SKILLS_DIR / ".usage"
PINNED_FILE = ".pinned"

STATE_ACTIVE = "active"
STATE_STALE = "stale"

DEFAULT_INTERVAL_HOURS = 24 * 5
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90
DEFAULT_CONSOLIDATE = False

# UI-configurable auto-maintenance interval override (in days). The client allows
# only 1..5 days; anything outside this range is rejected and the file-based
# `interval_hours` (default 5 days) is used.
DEFAULT_INTERVAL_OVERRIDE_MIN_DAYS = 1
DEFAULT_INTERVAL_OVERRIDE_MAX_DAYS = 5
