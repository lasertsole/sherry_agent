"""Agent switching: per-session toggles shared by gated middlewares.

The 预设-中间件 tab turns optional entries off by NAME
(``AGENT_CONFIG["middlewares_disabled"]``, written by
``PUT /sessions/agent_config``). The chain itself never changes — membership and
order are a build-time contract (``scaffolding`` + the order test) — so a
disabled entry stays registered and its hooks early-return here.

Reads are mem-only (the middleware runs inside a turn) and fail-open: an
unreadable register or a malformed payload means "everything on", because a
broken config must never silently strip behaviour.
"""

from __future__ import annotations

from loguru import logger

__all__ = ["disabled_middlewares", "middleware_enabled"]


def disabled_middlewares(session_id: str | None) -> frozenset[str]:
    """Names the session turned off (empty when unset / unreadable)."""
    if not session_id:
        return frozenset()
    try:
        from runtime import StateKey, state_register_mem

        raw = state_register_mem.get_state(session_id, StateKey.AGENT_CONFIG, None)
        if not isinstance(raw, dict):
            return frozenset()
        names = raw.get("middlewares_disabled")
        if not isinstance(names, list):
            return frozenset()
        return frozenset(str(name) for name in names if isinstance(name, str) and name)
    except Exception:  # noqa: BLE001 - a broken register must not break a turn
        logger.exception("agent_switch: failed to read the disabled-middleware set; enabling all")
        return frozenset()


def middleware_enabled(session_id: str | None, name: str) -> bool:
    """Whether *name* should run for this session (default: yes)."""
    return name not in disabled_middlewares(session_id)
