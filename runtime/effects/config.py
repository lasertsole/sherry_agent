"""Typed readers for the ``effects.*`` restart-recovery settings.

Every reader falls back to the registered default, so a ``sherry.jsonc`` that
never mentions ``effects`` behaves exactly like the pre-plan install. The
execution gate deliberately requires BOTH the master switch and the worker
flag, and is disabled while shadow mode is on: an operator who enables the
queue but not its worker keeps the original synchronous path.
"""

from __future__ import annotations

from config.sherry_settings import get_sherry_setting

__all__ = [
    "effects_enabled",
    "effects_execution_enabled",
    "effects_shadow_enabled",
    "max_attempts",
    "poll_interval_s",
    "restart_replay",
    "retry_initial_s",
    "retry_max_s",
    "shadow_mode",
    "worker_enabled",
]


def effects_enabled() -> bool:
    return bool(get_sherry_setting("effects.enabled"))


def shadow_mode() -> bool:
    return bool(get_sherry_setting("effects.shadow_mode"))


def worker_enabled() -> bool:
    return bool(get_sherry_setting("effects.worker_enabled"))


def restart_replay() -> bool:
    return bool(get_sherry_setting("effects.restart_replay"))


def poll_interval_s() -> int:
    return int(get_sherry_setting("effects.poll_interval_s"))


def retry_initial_s() -> int:
    return int(get_sherry_setting("effects.retry_initial_s"))


def retry_max_s() -> int:
    return int(get_sherry_setting("effects.retry_max_s"))


def max_attempts() -> int:
    return int(get_sherry_setting("effects.max_attempts"))


def effects_shadow_enabled() -> bool:
    """Master switch + observation mode (the worker may be off)."""
    return effects_enabled() and shadow_mode()


def effects_execution_enabled() -> bool:
    """Master switch + worker, and NOT shadow mode."""
    return effects_enabled() and worker_enabled() and not shadow_mode()
