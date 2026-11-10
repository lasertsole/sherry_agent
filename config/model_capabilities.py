"""Declarative model capability catalog (2026-11-10 image plan #0).

ZCode declares what each model accepts (images / PDF / video) in a versioned
JSON catalog; Sherry used to learn it purely by trying and caching the failure.
This module is the declarative half:

* ``config/model_capabilities.json`` is the tracked, versioned catalog (the
  plan said ``src/data`` but that tree is gitignored runtime data; the loader
  keeps the tracked catalog here and the learned labels in
  ``src/data/model_capabilities_runtime.json``).
* ``sherry.jsonc``'s ``model_capabilities`` object overrides the catalog per
  model.
* ``get_declared_capability()`` answers ``"supported"`` / ``"unsupported"`` /
  ``None`` (not declared).

The process-level cache (``agent/middlewares/llm_capability_cache.py``) calls
this first; only undeclared models fall back to reactive probing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal, TypedDict

__all__ = [
    "ModelCapabilities",
    "get_declared_capability",
    "reset_catalog_cache",
]

MediaKind = Literal["vision", "audio", "video", "pdf"]

_CATALOG_PATH = Path(__file__).with_name("model_capabilities.json")

_FIELD_BY_MEDIA: dict[str, str] = {
    "vision": "supports_images",
    "audio": "supports_audio",
    "video": "supports_video",
    "pdf": "supports_pdf",
}

_lock = threading.Lock()
_catalog: dict[str, dict] | None = None
_user_overrides: dict[str, dict] | None = None


class ModelCapabilities(TypedDict, total=False):
    """One catalog entry: the media families a model accepts."""

    supports_images: bool
    supports_audio: bool
    supports_video: bool
    supports_pdf: bool


def _read_object(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def _load_catalog_locked() -> dict[str, dict]:
    global _catalog
    if _catalog is None:
        _catalog = _read_object(_CATALOG_PATH)
    return _catalog


def _load_user_overrides_locked() -> dict[str, dict]:
    global _user_overrides
    if _user_overrides is not None:
        return _user_overrides
    try:
        from config.sherry_settings import load_sherry_settings

        raw = load_sherry_settings().get("model_capabilities")
    except Exception:  # noqa: BLE001 — a broken config must not disable the catalog
        raw = None
    _user_overrides = (
        {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}
        if isinstance(raw, dict)
        else {}
    )
    return _user_overrides


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return None


def get_declared_capability(provider: str, model: str, media: str) -> str | None:
    """Return ``"supported"`` / ``"unsupported"`` / ``None`` for one pair.

    User overrides (``sherry.jsonc``) merge over the tracked catalog. An
    unparseable field is ignored rather than treated as false.
    """
    field = _FIELD_BY_MEDIA.get(media)
    if field is None:
        return None
    key = f"{provider}/{model}"
    with _lock:
        catalog = _load_catalog_locked()
        overrides = _load_user_overrides_locked()
    entry = dict(catalog.get(key, {}))
    entry.update(overrides.get(key, {}))
    if field not in entry:
        return None
    value = _as_bool(entry[field])
    if value is None:
        return None
    return "supported" if value else "unsupported"


def reset_catalog_cache() -> None:
    """Drop the in-process catalog/override cache (tests or a live edit)."""
    global _catalog, _user_overrides
    with _lock:
        _catalog = None
        _user_overrides = None
