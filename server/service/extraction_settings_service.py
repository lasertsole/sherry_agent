"""Global + per-session extraction settings (预设角色 → 高级).

Four user-facing values, two scopes:

* **global** — ``sherry.jsonc``'s ``extraction`` group, read live by
  :mod:`runtime.session.extraction` and written through the existing
  ``/sherry-config`` machinery (typed coercion, comment-preserving line edit,
  ``.bak`` backup). Applies immediately to every session that has not
  overridden the field.
* **session** — ``StateKey.EXTRACTION`` (parked twin ``EXTRACTION_PENDING``),
  written under the shared session lock exactly like the model / thinking /
  agent-config controls: live while the session is idle, parked while a turn
  runs, promoted at the turn boundary by
  ``session_settings_service.promote_pending_settings_sync`` (its
  ``_SETTINGS_KEYS`` table includes the twin).

Only experience learning and the two iteration ceilings are session fields.
Flow replay compilation is global-only: a session payload that names it is
rejected by the sanitizer, so the UI's read-only row cannot be bypassed through
the API.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from pub.func.validator import is_safe_session_id
from runtime import StateKey
from runtime.session.extraction import (
    EXTRACTION_DEFAULTS,
    EXTRACTION_MAX_ITERATIONS,
    EXTRACTION_MIN_ITERATIONS,
    GLOBAL_FIELDS,
    ExtractionSettingsError,
    global_extraction_config,
    merge_extraction_values,
    normalize_session_overrides,
    sanitize_extraction_values,
)

__all__ = [
    "ExtractionSettingsError",
    "apply_session_extraction_choice",
    "get_extraction_state",
    "set_global_extraction",
]


def _bounds() -> dict[str, int]:
    """The iteration-ceiling bounds the UI renders (one source with the sanitizer)."""
    return {
        "min_iterations": EXTRACTION_MIN_ITERATIONS,
        "max_iterations": EXTRACTION_MAX_ITERATIONS,
    }


def get_extraction_state(session_id: str = "") -> dict[str, Any]:
    """The full state one Advanced tab renders.

    ``session`` is the payload the NEXT turn will use (a parked mid-turn choice
    wins), ``effective`` is that payload merged over the global values, and
    ``defaults`` lets the UI's reset action show the built-in numbers without a
    second hardcoded copy. Never raises for a malformed stored value: the
    normalizer drops what it cannot trust, which reads as "inherit".
    """
    from server.service.session_settings_service import _pending_value, _read_value

    if session_id and not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    global_values = global_extraction_config()
    overrides: dict[str, Any] = {}
    pending = False
    if session_id:
        parked, parked_value = _pending_value(session_id, StateKey.EXTRACTION_PENDING)
        pending = parked
        value = parked_value if parked else _read_value(session_id, StateKey.EXTRACTION)
        overrides = normalize_session_overrides(value)

    return {
        "global": global_values,
        "session": overrides,
        "effective": merge_extraction_values(global_values, overrides),
        "pending": pending,
        "defaults": dict(EXTRACTION_DEFAULTS),
        "bounds": _bounds(),
    }


async def apply_session_extraction_choice(
    session_id: str, values: object
) -> tuple[dict[str, Any], bool]:
    """HTTP entry for ``PUT /sessions/extraction``; returns ``(overrides, pending)``.

    The payload REPLACES the session's override set (absent/null fields fall
    back to the global values). Validated before any register is touched, then
    applied under the shared session lock so a mid-turn choice parks instead of
    changing the values the in-flight turn started with.
    """
    from server.service.session_settings_service import (
        _apply_session_choice,
        _delete_value,
        _write_value,
    )

    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    cleaned = sanitize_extraction_values(values, scope="session")

    def _write_live() -> None:
        """Straight to the live key: a live write supersedes a parked choice."""
        if cleaned:
            _write_value(session_id, StateKey.EXTRACTION, cleaned)
        else:
            _delete_value(session_id, StateKey.EXTRACTION)
        _delete_value(session_id, StateKey.EXTRACTION_PENDING)

    pending = await _apply_session_choice(
        session_id,
        live_key=StateKey.EXTRACTION,
        pending_key=StateKey.EXTRACTION_PENDING,
        value=cleaned or None,
        write_live=_write_live,
    )
    logger.info(
        "Extraction overrides {} for session {}: skill={} main={} sub={}",
        "parked" if pending else "applied",
        session_id,
        cleaned.get("skill_extraction_enabled"),
        cleaned.get("iteration_budget_main"),
        cleaned.get("iteration_budget_sub"),
    )
    return cleaned, pending


def set_global_extraction(values: object) -> dict[str, Any]:
    """HTTP entry for ``PUT /extraction/global``; returns the new global values.

    Only the fields present are written, so a partial payload cannot reset a
    sibling by accident. Values are coerced and validated first (a bad payload
    raises before the file is touched), then written through the existing
    ``write_sherry_config`` path — the same typed, comment-preserving edit the
    App Config panel uses.
    """
    from server.service.sherry_config import write_sherry_config

    cleaned = sanitize_extraction_values(values, scope="global")
    changes: dict[str, Any] = {
        f"extraction.{key}": cleaned[key] for key in GLOBAL_FIELDS if key in cleaned
    }
    if changes:
        write_sherry_config(changes)
    # A sub ceiling above the merged main is clamped on read; validate the
    # PAIR here as well so a partial write reports the value it actually acts
    # on instead of echoing an impossible pair.
    current = global_extraction_config()
    if "iteration_budget_sub" in cleaned and "iteration_budget_main" not in cleaned:
        current["iteration_budget_sub"] = min(
            current["iteration_budget_sub"], current["iteration_budget_main"]
        )
    logger.info(
        "Global extraction settings updated: skill={} flow={} main={} sub={}",
        current["skill_extraction_enabled"],
        current["flow_extraction_enabled"],
        current["iteration_budget_main"],
        current["iteration_budget_sub"],
    )
    return current
