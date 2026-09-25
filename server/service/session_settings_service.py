"""Per-session model thinking control state.

Backs the client's thinking control (``GET/PUT /sessions/thinking``). The flag
is dual-written to the in-memory register (what the agent-side middleware
reads on every model call, keeping blocking I/O off the event loop) and the
durable SQLite register (what survives a restart; reads rehydrate mem).

The stored value depends on the configured model's control mode
(:func:`thinking_control_mode`):

* ``"on_off"``  → ``True`` / ``False`` (a switch);
* ``"levels"``  → ``"low"`` / ``"high"`` / ``"max"`` (always-think models).

An absent flag = the user never made an explicit choice → the
``MAIN_LLM_ENABLE_THINKING`` env default applies. Writes are rejected while
the session has a turn in progress (``is_session_busy``) so a running turn
cannot switch model variants mid-flight.
"""

from agent.tools.subagent.registry.session_state import is_session_busy
from runtime import StateKey, state_register_db, state_register_mem
from pub.func.validator import is_safe_session_id

_VALID_LEVELS = ("low", "high", "max")


class SessionBusyError(RuntimeError):
    """Raised when the session has a turn in progress (HTTP maps to 409)."""


def _main_model_identity() -> tuple[str | None, str | None]:
    """(provider, model_name) of the configured main LLM."""
    from models.LLMs import main_llm as main_llm_module

    return main_llm_module.model_provider, main_llm_module.api_name


def get_thinking_mode() -> str:
    """The configured model's thinking control mode: "on_off" or "levels"."""
    from models.LLMs.reasoning_payload import thinking_control_mode

    provider, model_name = _main_model_identity()
    return thinking_control_mode(provider, model_name)


def _read_flag(session_id: str) -> object:
    value = state_register_mem.get_state(session_id, StateKey.LLM_THINKING_ENABLED, None)
    if value is not None:
        return value
    value = state_register_db.get_state(session_id, StateKey.LLM_THINKING_ENABLED, None)
    if value is not None:
        # Rehydrate mem so the agent-side middleware (mem-only reads) sees the
        # persisted flag without another db hit.
        state_register_mem.set_state(session_id, StateKey.LLM_THINKING_ENABLED, value)
    return value


def get_thinking_state(session_id: str) -> dict:
    """Return ``{"mode", "enabled", "level"}`` for the session.

    ``enabled`` is the explicit on/off choice (``None`` when unset); ``level``
    is the explicit low/high/max choice for level-selector models.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    value = _read_flag(session_id)
    if isinstance(value, bool):
        return {"mode": get_thinking_mode(), "enabled": value, "level": None}
    if isinstance(value, str) and value in _VALID_LEVELS:
        return {"mode": get_thinking_mode(), "enabled": None, "level": value}
    return {"mode": get_thinking_mode(), "enabled": None, "level": None}


def set_thinking_value(session_id: str, value: bool | str) -> None:
    """Persist the session's explicit thinking choice to both registers.

    Raises :class:`SessionBusyError` while the session has a turn in progress
    (the model variant must never change mid-turn), and ``ValueError`` for
    invalid sessions or values that contradict the model's control mode.
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    if is_session_busy(session_id):
        raise SessionBusyError("session has a turn in progress; thinking control is locked")

    mode = get_thinking_mode()
    if mode == "levels":
        if not (isinstance(value, str) and value in _VALID_LEVELS):
            raise ValueError(f"'value' must be one of {_VALID_LEVELS} for this model")
    elif not isinstance(value, bool):
        raise ValueError("'value' must be a boolean for this model")

    state_register_mem.set_state(session_id, StateKey.LLM_THINKING_ENABLED, value)
    state_register_db.set_state(session_id, StateKey.LLM_THINKING_ENABLED, value)


def get_thinking_enabled(session_id: str) -> bool | None:
    """Backward-compatible read of the boolean flag (on_off models)."""
    return get_thinking_state(session_id)["enabled"]


def set_thinking_enabled(session_id: str, enabled: bool) -> None:
    """Backward-compatible write of the boolean flag (on_off models)."""
    set_thinking_value(session_id, enabled)


async def set_thinking_value_guarded(session_id: str, value: bool | str) -> None:
    """Async entry for the HTTP layer: lock the session, check every busy
    signal, then validate and dual-write.

    Busy = ``detect_state`` signals (HITL wait, auto turn, WS task table) OR a
    live queue row (QUEUED/CLAIMED) — the placeholder that covers exactly the
    gap where a normal chat turn is executing without any detect_state signal.
    Holding the input queue's per-session lock makes the check-and-write
    atomic against a racing submit.
    """
    import asyncio

    from agent.tools.subagent.registry.session_state import normalize_session_key
    from server.queue.user_input_queue import UserInputQueueStatus
    from server.service.input_queue_service import _get_session_lock, get_default_queue

    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    async with _get_session_lock(session_id):
        if is_session_busy(session_id):
            raise SessionBusyError("session has a turn in progress; thinking control is locked")
        queue = get_default_queue()
        rows = await queue.list_active(normalize_session_key(session_id))
        if any(
            row.status in (UserInputQueueStatus.QUEUED, UserInputQueueStatus.CLAIMED)
            for row in rows
        ):
            raise SessionBusyError("session has a turn in progress; thinking control is locked")
        await asyncio.to_thread(set_thinking_value, session_id, value)
