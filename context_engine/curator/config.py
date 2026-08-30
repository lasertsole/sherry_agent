from typing import Any
from loguru import logger
from dotenv import load_dotenv

from config import ROOT_DIR, ENV_PATH
from context_engine.curator.constants import (
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_INTERVAL_OVERRIDE_MIN_DAYS,
    DEFAULT_INTERVAL_OVERRIDE_MAX_DAYS,
    DEFAULT_MIN_IDLE_HOURS,
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_CONSOLIDATE,
)

load_dotenv(ENV_PATH, override=True)

# Override interval is persisted in .curator_state under this key. A non-null
# value (clamped to [MIN, MAX] days) overrides the `interval_hours` from
# curator.yaml so the client's "auto maintenance interval" setting takes
# precedence over the file-based default (5 days).
_INTERVAL_OVERRIDE_KEY = "auto_interval_days"
# Last maintenance time (manual or auto) is surfaced to the client. Mirrors the
# transient `last_run_at` value but is always written on a successful run.
_LAST_MAINTENANCE_KEY = "last_maintenance_at"


def _load_config() -> dict[str, Any]:
    try:
        import yaml

        cfg_path = ROOT_DIR / "curator.yaml"
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if isinstance(cfg, dict):
                return cfg
    except Exception as e:
        logger.debug("Failed to load curator config: {}", e)
    return {}


def is_enabled() -> bool:
    return bool(_load_config().get("enabled", True))


def get_interval_hours() -> int:
    try:
        return int(_load_config().get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS


def get_min_idle_hours() -> float:
    try:
        return float(_load_config().get("min_idle_hours", DEFAULT_MIN_IDLE_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_MIN_IDLE_HOURS


def get_stale_after_days() -> int:
    try:
        return int(_load_config().get("stale_after_days", DEFAULT_STALE_AFTER_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_STALE_AFTER_DAYS


def get_archive_after_days() -> int:
    try:
        return int(_load_config().get("archive_after_days", DEFAULT_ARCHIVE_AFTER_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_ARCHIVE_AFTER_DAYS


def get_consolidate() -> bool:
    return bool(_load_config().get("consolidate", DEFAULT_CONSOLIDATE))


def _clamp_interval_days(days: Any) -> int | None:
    """Clamp a raw override value to the allowed 1..7 day range.

    Returns ``None`` for ``None``/empty/out-of-range inputs so the caller can
    treat it as "no override -> use curator.yaml interval_hours".
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
    """Return the UI-configured maintenance interval (days, 1..7) or None.

    Reads the override persisted in ``.curator_state`` under
    ``auto_interval_days``. Only values within the allowed 1..7 range are
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

    Pass ``None`` to clear the override and fall back to curator.yaml's
    ``interval_hours``. Returns the effective stored value after clamping.
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
    the ``interval_hours`` from curator.yaml (default 7 days = 168h).
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
