"""Skill-curator default intervals and retention windows."""

from typing import TypedDict


class CuratorDefaultsConfig(TypedDict):
    """Skill-curator default intervals and retention windows."""

    default_interval_hours: int
    default_min_idle_hours: int
    default_stale_after_days: int
    default_archive_after_days: int
    default_consolidate: bool
    interval_override_min_days: int
    interval_override_max_days: int
    http_interval_min_days: int
    http_interval_max_days: int


CURATOR_DEFAULTS: CuratorDefaultsConfig = {
    "default_interval_hours": 120,
    "default_min_idle_hours": 2,
    "default_stale_after_days": 30,
    "default_archive_after_days": 90,
    "default_consolidate": False,
    "interval_override_min_days": 1,
    "interval_override_max_days": 5,
    "http_interval_min_days": 1,
    "http_interval_max_days": 5,
}
