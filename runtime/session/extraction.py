"""Effective extraction settings — 经验学习 / 流程复用编译 / iteration ceilings.

One user-facing surface (预设角色 → 高级) controls four values, at two scopes:

* **global** — ``sherry.jsonc``'s ``extraction`` group, edited live through
  ``PUT /extraction/global``. This is the app-wide value every session
  inherits.
* **session** — ``StateKey.EXTRACTION`` overrides (parked twin
  ``EXTRACTION_PENDING``), written through ``PUT /sessions/extraction``. A
  session may override **experience learning** and the two **iteration
  ceilings**; **flow replay compilation** is global-only by design, so a
  session payload naming it is rejected (the plan's parent scope: whether
  replay scripts are generated is an infrastructure decision, not a
  per-conversation toggle).

Resolution order per field: session override → global value → built-in
default. The parent/child constraint is applied last: ``skill_extraction`` off
forces ``flow_extraction`` off, so a disabled experience pipeline can never
still compile replay scripts. The sub ceiling is also clamped to the main
ceiling (a child can never out-iterate its parent) and both are clamped to
their positive bounds, so a hand-edited config cannot disable the agent.

This module is the ONE reader the agent-side gates use (skill review, PTC
compile, LDW compile, IterationBudget). Every read is fail-open: a broken
config file or register yields the built-in defaults, never an exception on a
turn path.
"""

from __future__ import annotations

from typing import Any, Final

from config.sherry_settings import SHERRY_SETTING_DEFAULTS, load_sherry_settings
from runtime.session.state_keys import StateKey
from runtime.session.state_register import state_register_db, state_register_mem

__all__ = [
    "EXTRACTION_DEFAULTS",
    "EXTRACTION_MAX_ITERATIONS",
    "EXTRACTION_MIN_ITERATIONS",
    "ExtractionSettingsError",
    "effective_extraction_config",
    "flow_extraction_enabled",
    "global_extraction_config",
    "merge_extraction_values",
    "normalize_session_overrides",
    "root_session_id",
    "sanitize_extraction_values",
    "session_extraction_overrides",
]

#: Positive integer bounds for either iteration ceiling. A ceiling of 0 (or a
#: negative value) would stop the agent before its first model call, so the
#: floor is 1; the ceiling is a sanity bound far above any real budget.
EXTRACTION_MIN_ITERATIONS: Final[int] = 1
EXTRACTION_MAX_ITERATIONS: Final[int] = 1_000

#: Field order of the user-facing payload. ``flow_extraction_enabled`` is
#: global-only; the other three are session-overridable.
GLOBAL_FIELDS: Final[tuple[str, ...]] = (
    "skill_extraction_enabled",
    "flow_extraction_enabled",
    "iteration_budget_main",
    "iteration_budget_sub",
)
SESSION_FIELDS: Final[tuple[str, ...]] = (
    "skill_extraction_enabled",
    "iteration_budget_main",
    "iteration_budget_sub",
)

#: The built-in defaults, derived from the sherry.jsonc registry so the two
#: cannot drift. 180 = the main agent's model-call ceiling; 90 = the executor's
#: (a worker's own 60 still wins, because the sub value is a CAP).
EXTRACTION_DEFAULTS: Final[dict[str, Any]] = {
    "skill_extraction_enabled": bool(
        SHERRY_SETTING_DEFAULTS["extraction.skill_extraction_enabled"]
    ),
    "flow_extraction_enabled": bool(SHERRY_SETTING_DEFAULTS["extraction.flow_extraction_enabled"]),
    "iteration_budget_main": int(SHERRY_SETTING_DEFAULTS["extraction.iteration_budget_main"]),
    "iteration_budget_sub": int(SHERRY_SETTING_DEFAULTS["extraction.iteration_budget_sub"]),
}


class ExtractionSettingsError(ValueError):
    """A rejected values payload — the HTTP layer turns it into a 400."""


def _clamp_iterations(value: Any, fallback: int) -> int:
    """Coerce one ceiling into ``[MIN, MAX]``; a bad value keeps *fallback*."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(EXTRACTION_MIN_ITERATIONS, min(EXTRACTION_MAX_ITERATIONS, number))


def global_extraction_config() -> dict[str, Any]:
    """The app-wide values from ``sherry.jsonc`` (never raises).

    ``load_sherry_settings`` already merges the typed defaults, so a missing
    file/key reads as the built-in value; the exception guard covers an
    environment where the config module itself is half-initialized.
    """
    try:
        settings = load_sherry_settings()
    except Exception:  # noqa: BLE001 - a config read must never break a turn
        settings = dict(SHERRY_SETTING_DEFAULTS)
    main = _clamp_iterations(
        settings.get("extraction.iteration_budget_main"),
        EXTRACTION_DEFAULTS["iteration_budget_main"],
    )
    sub = min(
        _clamp_iterations(
            settings.get("extraction.iteration_budget_sub"),
            EXTRACTION_DEFAULTS["iteration_budget_sub"],
        ),
        main,
    )
    return {
        "skill_extraction_enabled": bool(
            settings.get(
                "extraction.skill_extraction_enabled",
                EXTRACTION_DEFAULTS["skill_extraction_enabled"],
            )
        ),
        "flow_extraction_enabled": bool(
            settings.get(
                "extraction.flow_extraction_enabled",
                EXTRACTION_DEFAULTS["flow_extraction_enabled"],
            )
        ),
        "iteration_budget_main": main,
        "iteration_budget_sub": sub,
    }


def normalize_session_overrides(value: object) -> dict[str, Any]:
    """Keep only the legal session-override fields of a stored payload.

    A malformed entry (a string ceiling, a bool stored in an integer field, an
    attempt to store ``flow_extraction_enabled``) is dropped, never raised:
    the register is durable state that may predate a validation change.
    """
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key in SESSION_FIELDS:
        if key not in value:
            continue
        raw = value[key]
        if key == "skill_extraction_enabled":
            if isinstance(raw, bool):
                cleaned[key] = raw
            continue
        if isinstance(raw, int) and not isinstance(raw, bool):
            cleaned[key] = raw
    return cleaned


def session_extraction_overrides(session_id: str) -> dict[str, Any]:
    """A session's explicit overrides (``{}`` = inherit the global values).

    Mem first (the agent-side hot read), then the durable register with a mem
    rehydrate — the same two-tier read the other per-session controls use. A
    malformed stored payload reads as no overrides (fail-open).
    """
    if not session_id:
        return {}
    value = state_register_mem.get_state(session_id, StateKey.EXTRACTION, None)
    if value is None:
        value = state_register_db.get_state(session_id, StateKey.EXTRACTION, None)
        if value is not None:
            state_register_mem.set_state(session_id, StateKey.EXTRACTION, value)
    return normalize_session_overrides(value)


def merge_extraction_values(
    global_values: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """Merge one session's overrides over the global values (the gate view).

    Session fields inherit field-by-field; flow replay compilation comes from
    the global scope only; the parent/child constraint and both clamps are
    applied here so every consumer sees the same effective values.
    """
    skill = bool(
        overrides.get("skill_extraction_enabled", global_values["skill_extraction_enabled"])
    )
    main = _clamp_iterations(
        overrides.get("iteration_budget_main", global_values["iteration_budget_main"]),
        global_values["iteration_budget_main"],
    )
    sub = min(
        _clamp_iterations(
            overrides.get("iteration_budget_sub", global_values["iteration_budget_sub"]),
            global_values["iteration_budget_sub"],
        ),
        main,
    )
    return {
        "skill_extraction_enabled": skill,
        "flow_extraction_enabled": bool(global_values["flow_extraction_enabled"]) and skill,
        "iteration_budget_main": main,
        "iteration_budget_sub": sub,
    }


def effective_extraction_config(session_id: str = "") -> dict[str, Any]:
    """Resolve the values the gates act on (never raises).

    A session may override experience learning and both ceilings. Flow replay
    compilation is read from the global scope only, and is forced off whenever
    experience learning is off (parent-off forces child-off).
    """
    global_values = global_extraction_config()
    try:
        overrides = session_extraction_overrides(session_id)
    except Exception:  # noqa: BLE001 - a register read must never break a turn
        overrides = {}
    return merge_extraction_values(global_values, overrides)


def flow_extraction_enabled(session_id: str = "") -> bool:
    """Shorthand for the compile gates (PTC / LDW)."""
    try:
        return bool(effective_extraction_config(session_id)["flow_extraction_enabled"])
    except Exception:  # noqa: BLE001 - fail-open to the historical behaviour
        return True


def sanitize_extraction_values(values: object, *, scope: str) -> dict[str, Any]:
    """Validate one write payload (raises :class:`ExtractionSettingsError`).

    ``scope="session"`` accepts only the three overridable fields and rejects
    ``flow_extraction_enabled`` by name (the global-only rule). ``scope="global"``
    accepts all four. A field whose value is ``null`` means "drop this override"
    and is simply omitted from the returned dict; an EMPTY payload is the legal
    "inherit every value" choice. Iteration ceilings are positive integers and
    ``sub`` is clamped to ``main``.
    """
    if not isinstance(values, dict):
        raise ExtractionSettingsError("values must be an object")
    allowed = SESSION_FIELDS if scope == "session" else GLOBAL_FIELDS
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        if scope == "session" and "flow_extraction_enabled" in unknown:
            raise ExtractionSettingsError(
                "flow_extraction_enabled is a global setting; a session cannot override it"
            )
        raise ExtractionSettingsError(f"unknown field(s): {', '.join(unknown)}")

    cleaned: dict[str, Any] = {}
    for key, raw in values.items():
        if raw is None:
            continue
        if key == "skill_extraction_enabled" or key == "flow_extraction_enabled":
            if not isinstance(raw, bool):
                raise ExtractionSettingsError(f"{key} must be a boolean")
            cleaned[key] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ExtractionSettingsError(f"{key} must be an integer")
        if not EXTRACTION_MIN_ITERATIONS <= raw <= EXTRACTION_MAX_ITERATIONS:
            raise ExtractionSettingsError(
                f"{key} must be between {EXTRACTION_MIN_ITERATIONS} and {EXTRACTION_MAX_ITERATIONS}"
            )
        cleaned[key] = raw

    if "iteration_budget_main" in cleaned and "iteration_budget_sub" in cleaned:
        cleaned["iteration_budget_sub"] = min(
            cleaned["iteration_budget_sub"], cleaned["iteration_budget_main"]
        )
    return cleaned


def root_session_id(session_id: str) -> str:
    """The MAIN session a (possibly nested) child session belongs to.

    Child keys are ``agent:<main-id>:subagent:<uuid>`` / ``agent:<main-id>:swarm:…``
    (see ``agent.tools.subagent.registry.session_keys``). A bare id is already
    the root, so it is returned unchanged.
    """
    sid = (session_id or "").strip()
    if sid.startswith("agent:"):
        parts = sid.split(":")
        if len(parts) >= 4 and parts[2] in ("subagent", "swarm"):
            return parts[1]
    return sid
