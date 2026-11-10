"""LLM multimodal capability cache — declarative catalog + process memory.

Key: ``"{provider}/{model_name}"``
Values per media type: ``"auto"`` (untested) | ``"supported"`` | ``"unsupported"``

Resolution order (2026-11-10 image plan #0):

1. the tracked ``config/model_capabilities.json`` catalog, overlaid by the
   ``model_capabilities`` object in ``sherry.jsonc`` — a declared model is
   answered without probing (ZCode's declarative path);
2. the learned runtime labels in ``src/data/model_capabilities_runtime.json``
   for undeclared models (one positive/negative probe is remembered across
   restarts).

The process-level cache survives across sessions but is reloaded from the
runtime file on first use after a restart. Consumers: ``media_pipeline``
(auto-mode decision) and ``llm_retry`` (records a rejection / a success).
"""

import json
import threading
from typing import Literal

from loguru import logger

from config.model_capabilities import get_declared_capability
from config.path import model_capabilities_runtime_path

_MediaType = Literal["vision", "audio", "video", "pdf"]
_CapabilityValue = Literal["auto", "supported", "unsupported"]

_MEDIA_TYPES = frozenset({"vision", "audio", "video", "pdf"})
_LEARNED_VALUES = frozenset({"supported", "unsupported"})

_lock = threading.Lock()
_cache: dict[str, dict[str, _CapabilityValue]] = {}
_runtime_loaded = False


def _load_runtime_locked() -> None:
    """Load the learned labels once per process (fail-open)."""
    global _runtime_loaded
    if _runtime_loaded:
        return
    _runtime_loaded = True
    try:
        raw = json.loads(model_capabilities_runtime_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    for model_key, per_media in raw.items():
        if not isinstance(model_key, str) or not isinstance(per_media, dict):
            continue
        clean = {
            media: value
            for media, value in per_media.items()
            if media in _MEDIA_TYPES and value in _LEARNED_VALUES
        }
        if clean:
            _cache.setdefault(model_key, {}).update(clean)


def _save_runtime_locked() -> None:
    """Persist the learned labels (declared models are not stored)."""
    learned = {
        model_key: {media: value for media, value in per_media.items() if value in _LEARNED_VALUES}
        for model_key, per_media in _cache.items()
    }
    learned = {key: value for key, value in learned.items() if value}
    try:
        from agent.tools.pub_base.atomic_write import atomic_write_text_no_follow

        path = model_capabilities_runtime_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text_no_follow(
            path, json.dumps(learned, ensure_ascii=False, indent=2, sort_keys=True)
        )
    except Exception as exc:  # noqa: BLE001 — a label write must never fail a turn
        logger.debug("Multimodal capability labels write failed (fail-open): {}", exc)


def get_capability(provider: str, model: str, media: _MediaType) -> _CapabilityValue:
    """Read the capability for a model+media pair ("auto" when unknowable).

    The declarative catalog (plus the user override) wins; only an undeclared
    model consults the learned runtime labels.
    """
    declared = get_declared_capability(provider, model, media)
    if declared == "supported":
        return "supported"
    if declared == "unsupported":
        return "unsupported"
    with _lock:
        _load_runtime_locked()
        return _cache.get(f"{provider}/{model}", {}).get(media, "auto")


def set_capability(provider: str, model: str, media: _MediaType, value: _CapabilityValue) -> None:
    """Write (or update) a learned capability. Thread-safe + persisted.

    A model the catalog/user already declares is left alone: a runtime probe
    must not silently override an explicit capability declaration (the user
    override in ``sherry.jsonc`` is the correction path).
    """
    if get_declared_capability(provider, model, media) is not None:
        logger.debug(
            "Multimodal capability ignored for declared model: provider={} model={} media={}",
            provider,
            model,
            media,
        )
        return
    model_key = f"{provider}/{model}"
    with _lock:
        _load_runtime_locked()
        per_media = _cache.setdefault(model_key, {})
        if per_media.get(media) == value:
            return
        per_media[media] = value
        _save_runtime_locked()
    logger.debug(
        "Multimodal capability cached: provider={} model={} media={} value={}",
        provider,
        model,
        media,
        value,
    )


def get_model_key() -> str:
    """Return "{provider}/{model_name}" for the currently configured main LLM.

    Reads MAIN_LLM_PROVIDER and MAIN_LLM_NAME env vars; an unset variable
    contributes an empty segment, so the key is never ambiguous across
    providers that happen to share a model name.
    """
    import os

    provider = os.getenv("MAIN_LLM_PROVIDER", "") or ""
    model_name = os.getenv("MAIN_LLM_NAME", "") or ""
    return f"{provider}/{model_name}"


def reset_cache() -> None:
    """Clear all learned capabilities (for testing or manual reset).

    The runtime file is NOT deleted; the next process start reloads it. Within
    this process the cleared state is authoritative until another write.
    """
    global _runtime_loaded
    with _lock:
        _cache.clear()
        _runtime_loaded = True


def reload_runtime_labels() -> None:
    """Drop the in-memory labels and re-read the runtime file (tests/manual)."""
    global _runtime_loaded
    with _lock:
        _cache.clear()
        _runtime_loaded = False
        _load_runtime_locked()
