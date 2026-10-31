"""Live safety-filter configuration (``sherry.jsonc`` ``safety_filter.*``).

Read on every hook through an mtime-cached snapshot: the 系统配置 → 直播安全 tab
writes those keys from a switch, and a settings switch that waits for a backend
restart is not a switch. The cache costs one ``stat`` per call in the steady
state, and the keyword rules file has its own mtime cache in :mod:`rules`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config.features import SAFETY_FILTER, SafetyFilterConfig
from config.sherry_settings import SHERRY_CONFIG_PATH, load_sherry_settings

__all__ = [
    "llm_moderation_active",
    "safety_filter_config",
    "safety_enabled",
]

_MODES = ("off", "on_keyword_hit", "always")

#: (mtime_ns, size) of the last read file → the parsed settings snapshot.
_cache_key: tuple[int, int] | None = None
_cache_settings: dict[str, Any] = dict(SAFETY_FILTER)


def _settings() -> dict[str, Any]:
    """The typed defaults overlaid with the live ``sherry.jsonc`` values."""
    global _cache_key, _cache_settings
    path: Path = SHERRY_CONFIG_PATH
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None and key == _cache_key:
        return _cache_settings
    merged: dict[str, Any] = dict(SAFETY_FILTER)
    try:
        live = load_sherry_settings()
    except Exception:  # noqa: BLE001 - a broken file must never break a model call
        live = {}
    mode = str(live.get("safety_filter.llm_moderation_mode", merged["llm_moderation_mode"]))
    merged["llm_moderation_mode"] = mode if mode in _MODES else "off"
    merged["enabled"] = bool(live.get("safety_filter.enabled", merged["enabled"]))
    merged["llm_moderation_model"] = str(
        live.get("safety_filter.llm_moderation_model", merged["llm_moderation_model"]) or ""
    ).strip()
    keyword_file = str(live.get("safety_filter.keyword_file", merged["keyword_file"]) or "")
    if keyword_file:
        merged["keyword_file"] = os.path.basename(keyword_file)
    _cache_key = key
    _cache_settings = merged
    return merged


def safety_filter_config() -> SafetyFilterConfig:
    """The live configuration (typed view of :func:`_settings`)."""
    return _settings()  # type: ignore[return-value]


def safety_enabled() -> bool:
    """Whether the feature's hooks do anything at all."""
    return bool(_settings().get("enabled"))


def llm_moderation_active() -> bool:
    """Whether an LLM review would actually run.

    Three conditions, all required: the master switch, a non-``off`` mode, and a
    configured review model. An empty model keeps the review off whatever the
    mode says — the 直播安全 tab's switch is disabled until one is typed.
    """
    settings = _settings()
    if not settings.get("enabled"):
        return False
    if settings.get("llm_moderation_mode") == "off":
        return False
    return bool(str(settings.get("llm_moderation_model") or "").strip())
