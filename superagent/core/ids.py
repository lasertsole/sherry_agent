"""Deterministic identifiers (prototype ``src/core/ids.py``)."""

from __future__ import annotations

import hashlib

from superagent.core.model import SourceRef

_SEPARATOR = "\x1f"


def key(*parts: str) -> str:
    """Return a stable 16-hex id; the separator prevents tuple collisions."""
    raw = _SEPARATOR.join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def thread_key(ref: SourceRef) -> str:
    """Return the canonical key for a source thread."""
    return key(ref.source, ref.id)


def trigger_key(source: str, raw_id: str) -> str:
    """Return the canonical id for a raw source message."""
    return key(source, raw_id)


def decision_key(thread: str, at: str, trigger_ids: list[str]) -> str:
    """Return the canonical id for a decision batch."""
    return key(thread, at, *trigger_ids)
