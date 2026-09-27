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
from collections.abc import Callable
from typing import Any

from agent.tools.subagent.registry.session_state import is_session_busy
from runtime import StateKey, state_register_db, state_register_mem
from pub.func.validator import is_safe_session_id

_VALID_LEVELS = ("low", "high", "max")

#: Descriptor fields accepted by ``PUT /sessions/model`` (unknown keys are
#: rejected so a typo in the client cannot silently store junk).
_OVERRIDE_FIELDS = ("id", "label", "provider", "model", "base_url", "api_key")
#: Upper bound per string field (credentials/urls stay far below this).
_MAX_FIELD_LEN = 512

#: (live key, pending key) twins for the two controls.
_SETTINGS_KEYS: tuple[tuple[StateKey, StateKey], ...] = (
    (StateKey.LLM_THINKING_ENABLED, StateKey.LLM_THINKING_ENABLED_PENDING),
    (StateKey.LLM_MAIN_MODEL, StateKey.LLM_MAIN_MODEL_PENDING),
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


def get_thinking_state(session_id: str) -> dict:
    """Return ``{"mode", "enabled", "level", "pending"}`` for the session.

    ``enabled`` is the explicit on/off choice (``None`` when unset); ``level``
    is the explicit low/high/max choice for level-selector models. Both
    describe the EFFECTIVE next-turn choice (a parked mid-turn selection is what
    the user just picked), while ``pending`` reports that it only lands on the
    next turn. ``mode`` follows the session's model (override-aware).
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if not _session_turn_active(session_id):
        promote_pending_settings_sync(session_id)
    mode = get_thinking_mode(session_id)
    parked, parked_value = _pending_value(session_id, StateKey.LLM_THINKING_ENABLED_PENDING)
    value = parked_value if parked else _read_value(session_id, StateKey.LLM_THINKING_ENABLED)
    if isinstance(value, bool):
        return {"mode": mode, "enabled": value, "level": None, "pending": parked}
    if isinstance(value, str) and value in _VALID_LEVELS:
        return {"mode": mode, "enabled": None, "level": value, "pending": parked}
    return {"mode": mode, "enabled": None, "level": None, "pending": parked}


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
