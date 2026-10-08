from typing import Any
from dotenv import load_dotenv

from config import ENV_PATH
from config.sherry_settings import get_sherry_setting
from context_engine.curator.constants import (
    DEFAULT_INTERVAL_OVERRIDE_MAX_DAYS,
    DEFAULT_INTERVAL_OVERRIDE_MIN_DAYS,
)

load_dotenv(ENV_PATH, override=True)

# Override interval is persisted in .curator_state under this key. A non-null
# value (clamped to [MIN, MAX] days) overrides the ``curator.interval_hours``
# setting in sherry.jsonc so the client's "auto maintenance interval" setting
# takes precedence over the file-based default (7 days).
_INTERVAL_OVERRIDE_KEY = "auto_interval_days"
# Last maintenance time (manual or auto) is surfaced to the client. Mirrors the
# transient `last_run_at` value but is always written on a successful run.
_LAST_MAINTENANCE_KEY = "last_maintenance_at"


def is_enabled() -> bool:
    return get_sherry_setting("curator.enabled")


def get_interval_hours() -> int:
    return get_sherry_setting("curator.interval_hours")


def get_min_idle_hours() -> float:
    return get_sherry_setting("curator.min_idle_hours")


def get_stale_after_days() -> int:
    return get_sherry_setting("curator.stale_after_days")


def get_archive_after_days() -> int:
    return get_sherry_setting("curator.archive_after_days")


def get_consolidate() -> bool:
    return get_sherry_setting("curator.consolidate")


def _clamp_interval_days(days: Any) -> int | None:
    """Clamp a raw override value to the allowed 1..5 day range.

    Returns ``None`` for ``None``/empty/out-of-range inputs so the caller can
    treat it as "no override -> use the ``curator.interval_hours`` setting
    from sherry.jsonc".
    """
    if days is None or days == "":
        return None
    try:
        value = int(days)
    except (TypeError, ValueError):
        return None
    if value < DEFAULT_INTERVAL_OVERRIDE_MIN_DAYS or value > DEFAULT_INTERVAL_OVERRIDE_MAX_DAYS:
        return None
    return value


def get_interval_override_days() -> int | None:
    """Return the UI-configured maintenance interval (days, 1..5) or None.

    Reads the override persisted in ``.curator_state`` under
    ``auto_interval_days``.     Only values within the allowed 1..5 range are
    considered a valid override.
    """
    try:
        from context_engine.curator.state import load_state

        state = load_state()
        return _clamp_interval_days(state.get(_INTERVAL_OVERRIDE_KEY))
    except Exception:
        return None


def set_interval_override_days(days: int | None) -> int | None:
    """Persist the UI-configured maintenance interval (days, clamped to 1..5).

    Pass ``None`` to clear the override and fall back to the
    ``curator.interval_hours`` setting in sherry.jsonc. Returns the effective
    stored value after clamping.
    """
    from context_engine.curator.state import load_state, save_state

    effective = _clamp_interval_days(days)
    state = load_state()
    state[_INTERVAL_OVERRIDE_KEY] = effective
    save_state(state)
    return effective


def get_effective_interval_hours() -> int:
    """Effective interval between curator runs, in hours.

    A UI-configured ``auto_interval_days`` override (1..5) takes precedence over
    the ``curator.interval_hours`` setting in sherry.jsonc (default
    7 days = 168h).
    """
    override = get_interval_override_days()
    if override is not None:
        return override * 24
    return get_interval_hours()


def get_last_maintenance_at() -> str | None:
    """Return the ISO timestamp of the last maintenance run, if any."""
    try:
        from context_engine.curator.state import load_state

        state = load_state()
        return state.get(_LAST_MAINTENANCE_KEY)
    except Exception:
        return None


def set_last_maintenance_at(value: str | None) -> None:
    """Persist the last maintenance timestamp to .curator_state."""
    from context_engine.curator.state import load_state, save_state

    state = load_state()
    state[_LAST_MAINTENANCE_KEY] = value
    save_state(state)
