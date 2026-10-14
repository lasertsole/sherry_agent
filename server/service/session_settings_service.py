"""Per-session main-model and thinking control state.

Backs the client's toolbar controls (``GET/PUT /sessions/thinking`` and
``GET/PUT /sessions/model``). Both values are dual-written to the in-memory
register (what the agent-side middleware reads on every model call, keeping
blocking I/O off the event loop) and the durable SQLite register (what survives
a restart; reads rehydrate mem).

Thinking (``StateKey.LLM_THINKING_ENABLED``) — the stored value depends on the
control mode of the model the session actually runs on
(:func:`thinking_control_mode`):

* ``"on_off"``  → ``True`` / ``False`` (a switch);
* ``"levels"``  → ``"low"`` / ``"high"`` / ``"max"`` (always-think models).

An absent flag = the user never made an explicit choice → the
``MAIN_LLM_ENABLE_THINKING`` env default applies.

Model (``StateKey.LLM_MAIN_MODEL``) — a profile descriptor
``{id, label, provider, model, base_url, api_key}`` the session runs on
instead of the env-configured main LLM (the client's env-config profiles);
absent = follow the env config. It composes with the thinking flag: the
middleware builds that provider/model with the session's thinking variant.

Mid-turn writes park instead of switching
-----------------------------------------
A turn runs many model calls, so applying a control change the moment it
arrives would swap the model/variant in the MIDDLE of that turn. Writes
therefore land in the live key while the session is idle, and in a sibling
``*_pending`` key while a turn is in flight — the running turn keeps the value
it started with. ``promote_pending_settings`` moves each parked value into the
live key once the turn ends (``turn_runner.on_turn_finished``), and an idle
read promotes too, so a parked value can never linger forever (restart, a
turn that ended without the hook). The pending entry is ``{"value": <value>}``
so a "cleared / follow the env config" choice parks distinctly from "nothing
parked"; ``value: null`` means clear on promotion.
"""

import asyncio
import re
from collections.abc import Callable
from typing import Any

from agent.tools.subagent.registry.session_state import is_session_busy
from runtime import StateKey, state_register_db, state_register_mem
from pub.func.validator import is_safe_session_id


def _valid_levels() -> tuple[str, ...]:
    """Every thinking-level token the API accepts (the shared 7-level enum).

    Derived from ``config.features.EXTENDED_THINKING_LEVELS`` so the value domain
    can never drift from the declarative engine's own level list.
    """
    from config.features import EXTENDED_THINKING_LEVELS

    return tuple(EXTENDED_THINKING_LEVELS)


_VALID_LEVELS = _valid_levels()

#: Descriptor fields accepted by ``PUT /sessions/model`` (unknown keys are
#: rejected so a typo in the client cannot silently store junk).
_OVERRIDE_FIELDS = ("id", "label", "provider", "model", "base_url", "api_key")
#: Upper bound per string field (credentials/urls stay far below this).
_MAX_FIELD_LEN = 512

#: (live key, pending key) twins for the per-session controls.
_SETTINGS_KEYS: tuple[tuple[StateKey, StateKey], ...] = (
    (StateKey.LLM_THINKING_ENABLED, StateKey.LLM_THINKING_ENABLED_PENDING),
    (StateKey.LLM_MAIN_MODEL, StateKey.LLM_MAIN_MODEL_PENDING),
    (StateKey.AGENT_CONFIG, StateKey.AGENT_CONFIG_PENDING),
    (StateKey.SEARCH_BACKEND, StateKey.SEARCH_BACKEND_PENDING),
    (StateKey.CUSTOM_THINKING_MAPPING, StateKey.CUSTOM_THINKING_MAPPING_PENDING),
)


def _main_model_identity() -> tuple[str | None, str | None]:
    """(provider, model_name) of the env-configured main LLM."""
    from models.LLMs import main_llm as main_llm_module

    return main_llm_module.model_provider, main_llm_module.api_name


def _read_value(session_id: str, key: str) -> Any:
    """Read a register value, rehydrating mem from the durable mirror."""
    value = state_register_mem.get_state(session_id, key, None)
    if value is not None:
        return value
    value = state_register_db.get_state(session_id, key, None)
    if value is not None:
        # Rehydrate mem so the agent-side middleware (mem-only reads) sees the
        # persisted value without another db hit.
        state_register_mem.set_state(session_id, key, value)
    return value


def _write_value(session_id: str, key: str, value: Any) -> None:
    """Dual-write a register value (mem + durable SQLite)."""
    state_register_mem.set_state(session_id, key, value)
    state_register_db.set_state(session_id, key, value)


def _delete_value(session_id: str, key: str) -> None:
    """Remove a register value from both registers."""
    state_register_mem.delete_state(session_id, key)
    state_register_db.delete_state(session_id, key)


def _pending_value(session_id: str, pending_key: str) -> tuple[bool, Any]:
    """``(is_parked, value)`` for a pending key; ``value: None`` = clear."""
    parked = _read_value(session_id, pending_key)
    if not isinstance(parked, dict) or "value" not in parked:
        return False, None
    return True, parked["value"]


def _park_value(session_id: str, pending_key: str, value: Any) -> None:
    """Park a value (``None`` = clear) for the next turn."""
    _write_value(session_id, pending_key, {"value": value})


def get_thinking_mode(session_id: str | None = None) -> str:
    """The session's model thinking-control mode: "on_off" or "levels"."""
    from models.LLMs.reasoning_payload import thinking_control_mode

    provider, model_name = _session_model_identity(session_id)
    return thinking_control_mode(provider, model_name)


def _session_model_identity(session_id: str | None) -> tuple[str | None, str | None]:
    """(provider, model_name) the given session will run on.

    The EFFECTIVE override counts: a parked mid-turn model choice already
    decides the mode the next turn will use (so the client can offer the right
    thinking control for the model the user just picked). The override's
    provider falls back to the env one, mirroring the builder.
    """
    if session_id:
        override = _effective_override(session_id)
        if override:
            provider = override.get("provider") or _main_model_identity()[0]
            return provider, override.get("model")
    return _main_model_identity()


def _effective_override(session_id: str) -> dict | None:
    """The override that will apply from the next turn (parked value wins)."""
    parked, value = _pending_value(session_id, StateKey.LLM_MAIN_MODEL_PENDING)
    raw = value if parked else _read_value(session_id, StateKey.LLM_MAIN_MODEL)
    if not isinstance(raw, dict) or not raw.get("model"):
        return None
    return raw


def _session_turn_active(session_id: str) -> bool:
    """Whether a turn is running (park decision for writes)."""
    try:
        if is_session_busy(session_id):
            return True
    except Exception:  # pragma: no cover - defensive, hooks may be unregistered
        return False
    return False


async def session_turn_active(session_id: str) -> bool:
    """Whether the session has a turn in flight — the durable, client-readable form.

    The in-memory signal alone is NOT enough: a normal chat stream never writes
    ``_active_tasks``/``ANSWERING``, so the live fact is a QUEUED/CLAIMED row in
    the input queue. Both are checked (the same pair the park decision uses), and
    the queue rows are read under the session lock so a submit racing this read
    cannot report "idle" for a turn that is about to start.

    ``GET /sessions/turn_state`` serves this: the composer uses it as a watchdog,
    so a client that missed the turn's terminal frame (socket swapped, frame
    dropped) still learns the turn is over instead of showing 停止 for ever.
    """
    from agent.tools.subagent.registry.session_state import normalize_session_key
    from server.queue.user_input_queue import UserInputQueueStatus
    from server.service.input_queue_service import _get_session_lock, get_default_queue

    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    async with _get_session_lock(session_id):
        if _session_turn_active(session_id):
            return True
        try:
            queue = get_default_queue()
            rows = await queue.list_active(normalize_session_key(session_id))
        except Exception:  # pragma: no cover - defensive, queue may be absent
            return False
        return any(
            row.status in (UserInputQueueStatus.QUEUED, UserInputQueueStatus.CLAIMED)
            for row in rows
        )


#: The level an always-think session starts at when the user never picked one
#: (mirrors the ``reasoning_effort`` default in ``models/LLMs/reasoning_payload.py``).
_DEFAULT_LEVEL = "high"


def _default_enabled() -> bool:
    """The env default (``MAIN_LLM_ENABLE_THINKING``) an unset session follows.

    Read live from the model module (its import-time global) so the payload can
    never disagree with what ``build_main_llm()`` actually does; a broken import
    degrades to ``False`` — the same fail-open posture as the rest of the module.
    """
    try:
        from models.LLMs import main_llm as main_llm_module

        return bool(main_llm_module.enable_thinking)
    except Exception:  # pragma: no cover - defensive
        return False


def _supported_levels(session_id: str) -> list[str]:
    """The thinking levels the session's EFFECTIVE model accepts, clamp order.

    Unmapped pairs report ``["off"]`` (the declarative engine's own fallback).
    The client renders its picker from this, so a level the model cannot take
    is never offered — the clamp then never has to fire in practice.
    """
    try:
        from models.LLMs.reasoning_payload import get_supported_thinking_levels

        provider, model_name = _session_model_identity(session_id)
        return get_supported_thinking_levels(provider, model_name)
    except Exception:  # noqa: BLE001 - a read model must not break on lookup
        # The loguru logger is imported lazily: the module keeps its import
        # surface small and this path must not fail on a missing logger either.
        import logging

        logging.getLogger(__name__).exception(
            "thinking levels lookup failed; offering the full enum"
        )
        return list(_VALID_LEVELS)


def get_thinking_state(session_id: str) -> dict:
    """Return ``{"mode", "enabled", "level", "pending"}`` for the session.

    ``enabled`` is the explicit on/off choice (``None`` when unset); ``level``
    is the explicit low/high/max choice for level-selector models. Both
    describe the EFFECTIVE next-turn choice (a parked mid-turn selection is what
    the user just picked), while ``pending`` reports that it only lands on the
    next turn. ``mode`` follows the session's model (override-aware).

    ``default_enabled`` / ``default_level`` carry what an UNSET control follows
    (the env flag and the level default). Without them a client has to guess the
    env default, and a hardcoded guess of ``false`` shows "thinking off" for a
    session whose model is still thinking — the mismatch that made a live session
    look like the switch had been ignored.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if not _session_turn_active(session_id):
        promote_pending_settings_sync(session_id)
    mode = get_thinking_mode(session_id)
    parked, parked_value = _pending_value(session_id, StateKey.LLM_THINKING_ENABLED_PENDING)
    value = parked_value if parked else _read_value(session_id, StateKey.LLM_THINKING_ENABLED)
    defaults = {
        "default_enabled": _default_enabled(),
        "default_level": _DEFAULT_LEVEL,
        "levels": _supported_levels(session_id),
    }
    if isinstance(value, bool):
        return {"mode": mode, "enabled": value, "level": None, "pending": parked, **defaults}
    if isinstance(value, str) and value in _VALID_LEVELS:
        return {"mode": mode, "enabled": None, "level": value, "pending": parked, **defaults}
    return {"mode": mode, "enabled": None, "level": None, "pending": parked, **defaults}


def set_thinking_value(session_id: str, value: bool | str) -> None:
    """Write the session's explicit thinking choice to the LIVE keys.

    Direct entry point (tests / idle callers); the HTTP layer goes through
    :func:`apply_thinking_choice`, which parks the value while a turn runs.
    ``ValueError`` for invalid sessions or values that contradict the model's
    control mode.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    mode = get_thinking_mode(session_id)
    if mode == "levels":
        if not (isinstance(value, str) and value in _VALID_LEVELS):
            raise ValueError(f"'value' must be one of {_VALID_LEVELS} for this model")
    elif not isinstance(value, bool):
        raise ValueError("'value' must be a boolean for this model")

    _write_value(session_id, StateKey.LLM_THINKING_ENABLED, value)
    # A live write supersedes any parked choice for the same control.
    _delete_value(session_id, StateKey.LLM_THINKING_ENABLED_PENDING)


def clear_thinking_value(session_id: str) -> None:
    """Drop the explicit thinking choice (back to the env default).

    Symmetric with :func:`set_main_model_override`'s ``None``: the live key is
    removed and a parked twin is dropped too, since "unset" is itself a choice.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    _delete_value(session_id, StateKey.LLM_THINKING_ENABLED)
    _delete_value(session_id, StateKey.LLM_THINKING_ENABLED_PENDING)


def get_thinking_enabled(session_id: str) -> bool | None:
    """Backward-compatible read of the boolean flag (on_off models)."""
    return get_thinking_state(session_id)["enabled"]


def set_thinking_enabled(session_id: str, enabled: bool) -> None:
    """Backward-compatible write of the boolean flag (on_off models)."""
    set_thinking_value(session_id, enabled)


def sanitize_main_model_override(descriptor: object) -> dict[str, str]:
    """Validate/normalize a profile descriptor (``ValueError`` on junk).

    Only the whitelisted string fields survive; ``model`` is mandatory, blank
    optional fields are dropped (so the builder falls back to the env value)
    and over-long values are rejected instead of being truncated into a
    different credential.
    """
    if not isinstance(descriptor, dict):
        raise ValueError("'profile' must be an object or null")
    unknown = sorted(set(descriptor) - set(_OVERRIDE_FIELDS))
    if unknown:
        raise ValueError(f"unknown profile field(s): {', '.join(unknown)}")
    model = descriptor.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("'profile.model' is required")
    cleaned: dict[str, str] = {"model": model.strip()}
    for field in _OVERRIDE_FIELDS:
        if field == "model":
            continue
        value = descriptor.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"'profile.{field}' must be a string")
        value = value.strip()
        if not value:
            continue
        if len(value) > _MAX_FIELD_LEN:
            raise ValueError(f"'profile.{field}' exceeds {_MAX_FIELD_LEN} characters")
        cleaned[field] = value
    return cleaned


def get_main_model_override(session_id: str, *, mask_secrets: bool = False) -> dict | None:
    """The session's EFFECTIVE main-model override (parked value wins).

    ``mask_secrets=True`` (the HTTP representation) drops ``api_key`` and
    reports ``has_api_key`` instead — credentials never travel back to the
    client, which already holds the profile it sent. The agent-side middleware
    reads the live key itself, so it never sees a parked value early.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    value = _effective_override(session_id)
    if value is None:
        return None
    if not mask_secrets:
        return dict(value)
    masked = {key: item for key, item in value.items() if key != "api_key"}
    masked["has_api_key"] = bool(value.get("api_key"))
    return masked


def set_main_model_override(session_id: str, descriptor: dict | None) -> None:
    """Write the session's main-model choice to the LIVE keys (``None`` clears).

    Direct entry point (tests / idle callers); the HTTP layer goes through
    :func:`apply_main_model_choice`, which parks the value while a turn runs.
    ``ValueError`` for invalid sessions/descriptors.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    if descriptor is None:
        _delete_value(session_id, StateKey.LLM_MAIN_MODEL)
        _delete_value(session_id, StateKey.LLM_MAIN_MODEL_PENDING)
        return

    cleaned = sanitize_main_model_override(descriptor)
    _write_value(session_id, StateKey.LLM_MAIN_MODEL, cleaned)
    # A live write supersedes any parked choice for the same control.
    _delete_value(session_id, StateKey.LLM_MAIN_MODEL_PENDING)


def get_main_model_state(session_id: str) -> dict:
    """Return ``{"override", "env_model", "pending"}`` for the session.

    ``override`` is the effective (parked-aware, credential-masked) profile,
    ``env_model`` the env identity the "follow env config" entry stands for,
    and ``pending`` whether the choice only lands on the next turn.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if not _session_turn_active(session_id):
        promote_pending_settings_sync(session_id)
    parked, _value = _pending_value(session_id, StateKey.LLM_MAIN_MODEL_PENDING)
    provider, model_name = _main_model_identity()
    return {
        "override": get_main_model_override(session_id, mask_secrets=True),
        "env_model": {"provider": provider, "model": model_name},
        "pending": parked,
    }


def promote_pending_settings_sync(session_id: str) -> list[str]:
    """Move every parked choice into its live key; returns the promoted keys.

    Idempotent and cheap when nothing is parked (one mem read per control).
    A parked ``value: null`` clears the live key. Called at turn end
    (:func:`promote_pending_settings`) and by idle reads, so a parked choice
    cannot linger after its turn.
    """
    promoted: list[str] = []
    for live_key, pending_key in _SETTINGS_KEYS:
        parked, value = _pending_value(session_id, pending_key)
        if not parked:
            continue
        if value is None:
            _delete_value(session_id, live_key)
        else:
            _write_value(session_id, live_key, value)
        _delete_value(session_id, pending_key)
        promoted.append(str(live_key))
        if live_key == StateKey.AGENT_CONFIG:
            # The promoted payload carries the skills selection, which lives
            # inside the cached system prompt — drop it so the next turn rebuilds
            # the <available_skills> index from the new value.
            from server.service.agent_config_service import invalidate_session_prompt

            invalidate_session_prompt(session_id)
    return promoted


async def promote_pending_settings(session_id: str) -> list[str]:
    """Async turn-end promotion (same work, off the event loop)."""
    if not session_id or not is_safe_session_id(session_id):
        return []
    return await asyncio.to_thread(promote_pending_settings_sync, session_id)


async def _apply_session_choice(
    session_id: str,
    *,
    live_key: StateKey,
    pending_key: StateKey,
    value: Any,
    write_live: Callable[[], None],
) -> bool:
    """Apply a control choice under the session lock; returns ``pending``.

    While a turn is in flight (any ``detect_state`` signal or a live queue row)
    the choice is PARKED so the running turn keeps its model/variant and the
    change lands on the next turn; otherwise it goes straight to the live keys.
    Holding the input queue's per-session lock keeps the check-and-write atomic
    against a racing submit.
    """
    from agent.tools.subagent.registry.session_state import normalize_session_key
    from server.queue.user_input_queue import UserInputQueueStatus
    from server.service.input_queue_service import _get_session_lock, get_default_queue

    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    async with _get_session_lock(session_id):
        busy = _session_turn_active(session_id)
        if not busy:
            try:
                queue = get_default_queue()
                rows = await queue.list_active(normalize_session_key(session_id))
                busy = any(
                    row.status in (UserInputQueueStatus.QUEUED, UserInputQueueStatus.CLAIMED)
                    for row in rows
                )
            except Exception:  # pragma: no cover - defensive, queue may be absent
                busy = False
        if busy:
            await asyncio.to_thread(_park_value, session_id, pending_key, value)
            return True
        await asyncio.to_thread(write_live)
        return False


async def apply_thinking_choice(session_id: str, value: bool | str | None) -> bool:
    """HTTP entry for ``PUT /sessions/thinking``; returns ``pending``.

    ``None`` clears the explicit choice (back to the env default). Otherwise the
    value is validated against the session's (effective) model mode, then
    written live or parked, so switching is allowed at any moment and takes
    effect on the next turn.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    if value is None:
        return await _apply_session_choice(
            session_id,
            live_key=StateKey.LLM_THINKING_ENABLED,
            pending_key=StateKey.LLM_THINKING_ENABLED_PENDING,
            value=None,
            write_live=lambda: clear_thinking_value(session_id),
        )

    mode = get_thinking_mode(session_id)
    if mode == "levels":
        if not (isinstance(value, str) and value in _VALID_LEVELS):
            raise ValueError(f"'value' must be one of {_VALID_LEVELS} for this model")
    elif not isinstance(value, bool):
        raise ValueError("'value' must be a boolean for this model")

    return await _apply_session_choice(
        session_id,
        live_key=StateKey.LLM_THINKING_ENABLED,
        pending_key=StateKey.LLM_THINKING_ENABLED_PENDING,
        value=value,
        write_live=lambda: set_thinking_value(session_id, value),
    )


#: ``wire_field`` = dotted lowercase path, at most 4 segments, 128 chars total.
_WIRE_FIELD_RE = re.compile(r"^[a-z_]+(\.[a-z_]+){0,3}$")
#: Level wire values: letters/digits/underscore/dash, 1-64 chars.
_WIRE_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_WIRE_FIELD_CHARS = 128


def _validate_custom_mapping(mapping: dict) -> dict:
    """Normalize + validate a custom thinking mapping (raises ``ValueError``).

    The mapping is the user's escape hatch into the request body, so every
    string that can reach the wire is constrained here: the field path to
    lowercase segments (no injection surface), the values to a conservative
    charset, the level keys to the shared enum, and the budgets to positive
    numbers under the model's context window.
    """
    from config.features import EXTENDED_THINKING_LEVELS, THINKING_LEVELS
    from models.LLMs.reasoning_payload import _FORMAT_LEVEL_MAPS  # noqa: PLC0415 - leaf read

    wire_format = mapping.get("wire_format") or "custom"
    if not isinstance(wire_format, str):
        raise ValueError("'wire_format' must be a string")
    known_formats = set(_FORMAT_LEVEL_MAPS) | {"custom"}
    if wire_format not in known_formats:
        raise ValueError(f"'wire_format' must be one of {sorted(known_formats)}")

    wire_field = mapping.get("wire_field") or ""
    if not isinstance(wire_field, str):
        raise ValueError("'wire_field' must be a string")
    wire_field = wire_field.strip()
    if wire_field:
        if len(wire_field) > _MAX_WIRE_FIELD_CHARS or not _WIRE_FIELD_RE.match(wire_field):
            raise ValueError(
                "'wire_field' must be a dotted lowercase path (letters/underscores), "
                "at most 4 segments"
            )
    if wire_format == "custom" and not wire_field:
        raise ValueError("'wire_field' is required when 'wire_format' is 'custom'")

    raw_map = mapping.get("level_map") or {}
    if not isinstance(raw_map, dict):
        raise ValueError("'level_map' must be an object")
    level_map: dict[str, str | None] = {}
    for key, value in raw_map.items():
        if key not in EXTENDED_THINKING_LEVELS:
            raise ValueError(f"'level_map' keys must be thinking levels, got {key!r}")
        if value is None or value == "":
            level_map[key] = None
            continue
        if not isinstance(value, str) or not _WIRE_VALUE_RE.match(value):
            raise ValueError(
                f"'level_map[{key}]' must be letters/digits/underscore/dash, 1-64 chars"
            )
        level_map[key] = value
    if not any(v is not None for v in level_map.values()):
        raise ValueError("'level_map' must declare at least one level")

    cleaned: dict = {"wire_field": wire_field, "wire_format": wire_format, "level_map": level_map}

    raw_budgets = mapping.get("thinking_budgets") or {}
    if raw_budgets:
        if not isinstance(raw_budgets, dict):
            raise ValueError("'thinking_budgets' must be an object")
        from models.LLMs import main_llm as _main_llm

        ceiling = int(getattr(_main_llm, "max_tokens", 0) or 0) or 131072
        budgets: dict[str, int] = {}
        for key, value in raw_budgets.items():
            if key not in THINKING_LEVELS:
                raise ValueError(f"'thinking_budgets' keys must be effort levels, got {key!r}")
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"'thinking_budgets[{key}]' must be a positive integer")
            if ceiling and value >= ceiling:
                raise ValueError(f"'thinking_budgets[{key}]' must stay under the context window")
            budgets[key] = value
        if budgets:
            cleaned["thinking_budgets"] = budgets
    return cleaned


def get_custom_thinking_mapping(session_id: str) -> dict | None:
    """The session's custom thinking mapping (parked value wins), or None."""
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if not _session_turn_active(session_id):
        promote_pending_settings_sync(session_id)
    parked, parked_value = _pending_value(session_id, StateKey.CUSTOM_THINKING_MAPPING_PENDING)
    value = parked_value if parked else _read_value(session_id, StateKey.CUSTOM_THINKING_MAPPING)
    if isinstance(value, dict) and value.get("level_map"):
        return {"pending": parked, **value}
    return None


def clear_custom_thinking_mapping(session_id: str) -> None:
    """Drop the session's custom mapping from both register tiers."""
    state_register_mem.delete_state(session_id, StateKey.CUSTOM_THINKING_MAPPING)
    state_register_db.delete_state(session_id, StateKey.CUSTOM_THINKING_MAPPING)


def set_custom_thinking_mapping(session_id: str, mapping: dict) -> None:
    """Write the custom mapping to the LIVE keys (idle/tests callers)."""
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    state_register_mem.set_state(session_id, StateKey.CUSTOM_THINKING_MAPPING, mapping)
    state_register_db.set_state(session_id, StateKey.CUSTOM_THINKING_MAPPING, mapping)


async def apply_custom_thinking_mapping(session_id: str, mapping: dict | None) -> bool:
    """HTTP entry for ``PUT /sessions/thinking/custom``; returns ``pending``.

    ``None`` clears the mapping (back to the declarative defaults). Otherwise
    the payload is validated, then written live or parked — the same
    next-turn semantics as every other per-session control.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    if mapping is None:
        return await _apply_session_choice(
            session_id,
            live_key=StateKey.CUSTOM_THINKING_MAPPING,
            pending_key=StateKey.CUSTOM_THINKING_MAPPING_PENDING,
            value=None,
            write_live=lambda: clear_custom_thinking_mapping(session_id),
        )

    cleaned = _validate_custom_mapping(mapping)
    return await _apply_session_choice(
        session_id,
        live_key=StateKey.CUSTOM_THINKING_MAPPING,
        pending_key=StateKey.CUSTOM_THINKING_MAPPING_PENDING,
        value=cleaned,
        write_live=lambda: set_custom_thinking_mapping(session_id, cleaned),
    )


async def apply_search_backend_choice(session_id: str, backend: str | None) -> bool:
    """HTTP entry for ``PUT /sessions/search_backend``; returns ``pending``.

    ``None`` clears the override (back to the availability-aware global
    default). Otherwise the value must be one of the two backend names.
    """
    if backend is not None and backend not in ("tavily", "searxng"):
        raise ValueError("'backend' must be 'tavily', 'searxng', or null")

    return await _apply_session_choice(
        session_id,
        live_key=StateKey.SEARCH_BACKEND,
        pending_key=StateKey.SEARCH_BACKEND_PENDING,
        value=backend,
        write_live=lambda: _write_value(session_id, StateKey.SEARCH_BACKEND, backend),
    )


def _search_env_view() -> dict[str, Any]:
    """The network.jsonc file as the 模型联网 panel edits it.

    Those keys live in ``network.jsonc`` (they moved out of ``.env``) and are
    WITHHELD from ``GET /env`` / ``GET /network-config`` (a dedicated panel owns
    them), so the panel reads them here instead. They are the FILE values — what
    a save would produce and what the next boot reads — while the sibling
    ``searxng`` / ``tavily_available`` fields report what THIS process loaded,
    which is how the panel can say "saved, restart to apply".
    """
    from server.service.network_config import network_file_values

    values = network_file_values()
    return {
        "searxng_url": values.get("SEARXNG_URL", ""),
        "searxng_proxy": values.get("SEARXNG_PROXY", ""),
        "searxng_engines": values.get("SEARXNG_ENGINES", ""),
        "searxng_categories": values.get("SEARXNG_CATEGORIES", ""),
        "searxng_verify_tls": values.get("SEARXNG_VERIFY_TLS", "true").strip().lower()
        not in ("0", "false", "no", "off"),
        #: A Tavily key sits in the file (the VALUE never leaves the server).
        "tavily_key_present": bool(values.get("TAVILY_API_KEY", "").strip()),
    }


def get_search_backend_state(session_id: str) -> dict[str, Any]:
    """The panel's read model: the session's choice, what is effective, and what
    each provider's configuration looks like.

    ``backend`` is the EFFECTIVE next-turn choice — a parked mid-turn selection
    counts (that is what the user just picked), with ``pending`` saying it only
    lands next turn. ``effective`` is what the running tool serves from now,
    i.e. the availability-aware resolution over the LIVE keys.

    An EMPTY session id is legal — 模型联网 is a settings tab and opens with no
    session too; it then answers the availability + default view (no explicit
    choice to report), which is what the panel's per-provider rows need.
    """
    from agent.tools.web_search import resolve_preferred_backend, tavily_available
    from agent.tools.web_search_searxng import searxng_status

    if session_id and not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    searxng = searxng_status()
    env_view = _search_env_view()
    if not session_id:
        return {
            "backend": None,
            "effective": resolve_preferred_backend(""),
            "pending": False,
            "pending_value": None,
            "tavily_available": tavily_available(),
            "searxng": searxng,
            "env": env_view,
        }

    # A park cannot outlive its turn (restart, missed hook): an idle read settles it.
    if not _session_turn_active(session_id):
        promote_pending_settings_sync(session_id)
    parked, parked_value = _pending_value(session_id, StateKey.SEARCH_BACKEND_PENDING)
    chosen = parked_value if parked else _read_value(session_id, StateKey.SEARCH_BACKEND)
    chosen = chosen if chosen in ("tavily", "searxng") else None
    return {
        "backend": chosen,
        "effective": resolve_preferred_backend(session_id),
        "pending": parked,
        "pending_value": parked_value,
        "tavily_available": tavily_available(),
        "searxng": searxng,
        "env": env_view,
    }


async def apply_main_model_choice(session_id: str, descriptor: dict | None) -> bool:
    """HTTP entry for ``PUT /sessions/model``; returns ``pending``.

    ``None`` clears the override (follow the env config) — also parkable, so a
    clear issued mid-turn lands on the next turn too.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if descriptor is not None:
        descriptor = sanitize_main_model_override(descriptor)
    return await _apply_session_choice(
        session_id,
        live_key=StateKey.LLM_MAIN_MODEL,
        pending_key=StateKey.LLM_MAIN_MODEL_PENDING,
        value=descriptor,
        write_live=lambda: set_main_model_override(session_id, descriptor),
    )
