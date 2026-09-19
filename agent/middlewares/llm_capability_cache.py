"""LLM multimodal capability cache — process-level in-memory dict.

Key: ``"{provider}/{model_name}"``
Values per media type: ``"auto"`` (untested) | ``"supported"`` | ``"unsupported"``

The cache survives across sessions within a single process but is lost on
restart. Model switches require env changes + restart, so the cache is
naturally clean for the new model; the cost of one wasted API call per restart
is negligible. Consumers: ``media_pipeline`` (auto-mode decision) and
``llm_retry`` (records a rejection reported by the model).
"""

import os
import threading
from typing import Literal

from loguru import logger

_MediaType = Literal["vision", "audio", "video"]
_CapabilityValue = Literal["auto", "supported", "unsupported"]

_lock = threading.Lock()
_cache: dict[str, dict[str, _CapabilityValue]] = {}


def get_capability(provider: str, model: str, media: _MediaType) -> _CapabilityValue:
    """Read the cached capability for a model+media pair. Returns "auto" if unknown."""
    with _lock:
        return _cache.get(f"{provider}/{model}", {}).get(media, "auto")


def set_capability(provider: str, model: str, media: _MediaType, value: _CapabilityValue) -> None:
    """Write (or update) the capability cache. Thread-safe."""
    with _lock:
        per_media = _cache.setdefault(f"{provider}/{model}", {})
        if per_media.get(media) == value:
            return
        per_media[media] = value
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
    provider = os.getenv("MAIN_LLM_PROVIDER", "") or ""
    model_name = os.getenv("MAIN_LLM_NAME", "") or ""
    return f"{provider}/{model_name}"


def reset_cache() -> None:
    """Clear all cached capabilities (for testing or manual reset)."""
    with _lock:
        _cache.clear()
